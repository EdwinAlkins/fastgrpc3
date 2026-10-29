"""`GrpcService`: handlers declared with decorators, checked when the module is imported.

    service = GrpcService("Orders", package="orders.v1")

    @service.unary("GetOrder")
    async def get_order(req: GetOrderRequest, ctx: Context, repo=Depends(get_repo)) -> Order:
        ...

The request parameter is the one annotated with a model (or `AsyncIterator[Model]` for a
client stream), `Context` and `Depends(...)` parameters are filled by the framework, and the
return annotation gives the response model (`AsyncIterator[Model]` for a server stream).
"""

import collections.abc
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from fastgrpc3.api.context import Context
from fastgrpc3.api.deps import Dependency, inspect_parameters
from fastgrpc3.schema import MethodDef, ServiceDef
from fastgrpc3.schema.registry import DEFAULT_PACKAGE

__all__ = ["GrpcService", "Handler"]

_STREAMS = (
    collections.abc.AsyncIterator,
    collections.abc.AsyncIterable,
    collections.abc.AsyncGenerator,
)
_SHAPES = {
    "unary": (False, False),
    "unary_stream": (False, True),
    "stream_unary": (True, False),
    "stream_stream": (True, True),
}

Function = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class Handler:
    """A decorated function and what its signature says."""

    method: MethodDef
    function: Function
    request_parameter: str
    context_parameter: str | None
    dependencies: dict[str, Dependency]


class GrpcService:
    """A gRPC service: a name, a package, and one handler per method."""

    def __init__(self, name: str, package: str = DEFAULT_PACKAGE, *, doc: str | None = None):
        self.name = name
        self.package = package
        self.doc = doc
        self.handlers: dict[str, Handler] = {}
        self._contract: ServiceDef | None = None

    @classmethod
    def implement(cls, contract: ServiceDef) -> "GrpcService":
        """A service for a generated contract: handlers must match its methods."""
        service = cls(contract.name, contract.package, doc=contract.doc)
        service._contract = contract
        return service

    @property
    def full_name(self) -> str:
        return f"{self.package}.{self.name}"

    def unary(self, name: str | None = None) -> Callable[[Function], Function]:
        """Register a request -> response handler (`name` defaults to the function's)."""
        return self._decorator("unary", name)

    def unary_stream(self, name: str | None = None) -> Callable[[Function], Function]:
        """Register a request -> stream of responses handler."""
        return self._decorator("unary_stream", name)

    def stream_unary(self, name: str | None = None) -> Callable[[Function], Function]:
        """Register a stream of requests -> response handler."""
        return self._decorator("stream_unary", name)

    def stream_stream(self, name: str | None = None) -> Callable[[Function], Function]:
        """Register a bidirectional streaming handler."""
        return self._decorator("stream_stream", name)

    def service_def(self) -> ServiceDef:
        """The contract of the service, from its handlers."""
        if self._contract is not None:
            missing = [m.name for m in self._contract.methods if m.name not in self.handlers]
            if missing:
                raise TypeError(f"{self.full_name}: no handler for {', '.join(missing)}")
            return self._contract
        methods = tuple(handler.method for handler in self.handlers.values())
        return ServiceDef(self.name, self.package, methods, self.doc)

    def _decorator(self, kind: str, name: str | None) -> Callable[[Function], Function]:
        def register(function: Function) -> Function:
            handler = analyze_handler(function, name or function.__name__, kind)
            if handler.method.name in self.handlers:
                raise TypeError(f"{self.full_name}: method {handler.method.name} declared twice")
            if self._contract is not None:
                _check_contract(self._contract, handler.method)
            self.handlers[handler.method.name] = handler
            return function  # unchanged: still callable in unit tests

        return register


def analyze_handler(function: Function, name: str, kind: str) -> Handler:
    """Read a handler's signature; refuse anything the framework could not call."""
    where = f"handler {name} ({getattr(function, '__qualname__', function)})"
    client_streaming, server_streaming = _SHAPES[kind]
    if server_streaming and not inspect.isasyncgenfunction(function):
        raise TypeError(f"{where}: a server stream handler is an async generator")
    if not server_streaming and not inspect.iscoroutinefunction(function):
        raise TypeError(f"{where}: a handler is an `async def` function")

    request: tuple[str, type[BaseModel]] | None = None
    context_parameter = None
    dependencies: dict[str, Dependency] = {}
    for parameter in inspect_parameters(function):
        if parameter.depends is not None:
            dependencies[parameter.name] = parameter.depends
            continue
        if parameter.annotation is Context:
            context_parameter = parameter.name
            continue
        model, streamed = _message_type(parameter.annotation)
        if model is None or request is not None or parameter.has_default:
            raise TypeError(
                f"{where}: parameter {parameter.name!r} is not the request, a Context "
                "or a Depends(...)"
            )
        if streamed != client_streaming:
            expected = "AsyncIterator[Model]" if client_streaming else "a model"
            raise TypeError(f"{where}: the request parameter must be {expected}")
        request = parameter.name, model
    if request is None:
        raise TypeError(f"{where}: no request parameter (annotated with a model)")

    hints = inspect_return(function)
    response, streamed = _message_type(hints)
    if response is None or streamed != server_streaming:
        expected = "AsyncIterator[Model]" if server_streaming else "a model"
        raise TypeError(f"{where}: the return annotation must be {expected}")
    method = MethodDef(
        name,
        request[1],
        response,
        client_streaming,
        server_streaming,
        inspect.getdoc(function),
    )
    return Handler(method, function, request[0], context_parameter, dependencies)


def inspect_return(function: Function) -> Any:
    """The resolved return annotation of `function`."""
    try:
        return typing.get_type_hints(function).get("return")
    except (NameError, TypeError) as e:
        raise TypeError(f"cannot resolve the annotations of {function.__qualname__}: {e}") from e


def _message_type(annotation: Any) -> tuple[type[BaseModel] | None, bool]:
    """`Model` -> (Model, False); `AsyncIterator[Model]` -> (Model, True); else (None, _)."""
    streamed = get_origin(annotation) in _STREAMS
    if streamed:
        annotation = get_args(annotation)[0] if get_args(annotation) else None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, streamed
    return None, streamed


def _check_contract(contract: ServiceDef, method: MethodDef) -> None:
    expected = next((m for m in contract.methods if m.name == method.name), None)
    if expected is None:
        raise TypeError(f"{contract.full_name} has no method {method.name}")
    ours = (method.request, method.response, method.client_streaming, method.server_streaming)
    theirs = (
        expected.request,
        expected.response,
        expected.client_streaming,
        expected.server_streaming,
    )
    if ours != theirs:
        raise TypeError(
            f"{contract.full_name}/{method.name}: the handler's signature does not match the "
            f"contract ({expected.request.__name__} -> {expected.response.__name__}, "
            f"client stream {expected.client_streaming}, server stream {expected.server_streaming})"
        )
