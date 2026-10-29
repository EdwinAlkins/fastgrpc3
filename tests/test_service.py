"""Handler signatures are checked when they are decorated."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from fastgrpc3 import Context, Depends, GrpcService, MethodDef, ServiceDef


class Req(BaseModel):
    n: int = 0


class Rep(BaseModel):
    n: int = 0


def test_name_defaults_to_the_function_name_and_the_function_is_kept() -> None:
    service = GrpcService("S", package="s.v1")

    async def GetThing(req: Req, ctx: Context, extra: int = Depends(lambda: 1)) -> Rep:
        """Get a thing."""
        return Rep()

    assert service.unary()(GetThing) is GetThing
    handler = service.handlers["GetThing"]
    assert handler.method == MethodDef("GetThing", Req, Rep, doc="Get a thing.")
    assert (handler.request_parameter, handler.context_parameter) == ("req", "ctx")
    assert list(handler.dependencies) == ["extra"]
    assert service.service_def() == ServiceDef("S", "s.v1", (handler.method,))


async def _unary(req: Req) -> Rep:
    return Rep()


async def _client_stream(req: AsyncIterator[Req]) -> Rep:
    return Rep()


async def _server_stream(req: Req) -> AsyncIterator[Rep]:
    yield Rep()


async def _no_request(ctx: Context) -> Rep:
    return Rep()


async def _two_requests(a: Req, b: Req) -> Rep:
    return Rep()


async def _unknown_parameter(req: Req, other: str) -> Rep:
    return Rep()


async def _bad_return(req: Req) -> int:
    return 0


def _not_async(req: Req) -> Rep:
    return Rep()


async def _two_contexts(req: Req, first: Context, second: Context) -> Rep:
    return Rep()


async def _positional_only(req: Req, /, ctx: Context) -> Rep:
    return Rep()


async def _var_keyword(req: Req, **extra: Any) -> Rep:
    return Rep()


@pytest.mark.parametrize(
    ("kind", "function", "message"),
    [
        ("unary", _client_stream, "request parameter must be a model"),
        ("stream_unary", _unary, r"must be AsyncIterator\[Model\]"),
        ("unary", _server_stream, "is an `async def` function"),
        ("unary_stream", _unary, "is an async generator"),
        ("unary", _no_request, "no request parameter"),
        ("unary", _two_requests, "parameter 'b' is not the request"),
        ("unary", _unknown_parameter, "parameter 'other' is not the request"),
        ("unary", _bad_return, "return annotation must be a model"),
        ("unary", _not_async, "is an `async def` function"),
        ("unary", _two_contexts, "two Context parameters"),
        ("unary", _positional_only, "parameter 'req' cannot be passed by name"),
        ("unary", _var_keyword, "parameter 'extra' cannot be passed by name"),
    ],
)
def test_wrong_signatures_are_refused(kind: str, function: Any, message: str) -> None:
    service = GrpcService("S", package="s.v1")
    with pytest.raises(TypeError, match=message):
        getattr(service, kind)()(function)


def test_a_method_is_declared_once() -> None:
    service = GrpcService("S", package="s.v1")
    service.unary("A")(_unary)
    with pytest.raises(TypeError, match="declared twice"):
        service.unary("A")(_unary)


def test_implementing_a_contract_checks_the_handlers() -> None:
    contract = ServiceDef(
        "S", "s.v1", (MethodDef("Get", Req, Rep), MethodDef("Watch", Req, Rep, False, True))
    )
    service = GrpcService.implement(contract)
    service.unary("Get")(_unary)
    with pytest.raises(TypeError, match="no handler for Watch"):
        service.service_def()
    with pytest.raises(TypeError, match="has no method Other"):
        service.unary("Other")(_unary)
    with pytest.raises(TypeError, match="does not match the contract"):
        service.unary("Watch")(_unary)
    service.unary_stream("Watch")(_server_stream)
    assert service.service_def() is contract
