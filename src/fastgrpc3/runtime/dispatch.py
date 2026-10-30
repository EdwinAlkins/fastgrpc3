"""From request bytes to response bytes: hooks, decoding, dependencies, errors.

Everything that can be decided once is decided when the server starts (codecs, dependency
plan, whether a `Context` is needed). The body stays bytes until the hooks (authentication)
have run. Failures become an `RpcFailure` the transport sends as a status:

- `GrpcError` -> its code, message and details;
- a request that does not decode -> `INVALID_ARGUMENT "invalid request payload"`;
- an exception with a registered handler -> the `GrpcError` it returns;
- anything else -> logged, then `INTERNAL "internal server error"` (nothing leaks).
"""

import contextlib
import inspect
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from fastgrpc3.api.context import Context
from fastgrpc3.api.deps import DependencyPlan
from fastgrpc3.api.errors import GrpcError, StatusCode, encode_details
from fastgrpc3.api.service import Handler
from fastgrpc3.runtime.deadline import reset_deadline, set_deadline
from fastgrpc3.schema.codec import MessageCodec
from fastgrpc3.transport.base import RpcFailure, TransportContext

__all__ = ["Call", "ExceptionHandler", "Hook", "Invoker", "Middleware", "Pipeline"]

logger = logging.getLogger("fastgrpc3")

Hook = Callable[[Context], Awaitable[None]]
CallNext = Callable[[], Awaitable[Any]]
Middleware = Callable[[Context, CallNext], Awaitable[Any]]
ExceptionHandler = Callable[[Exception], GrpcError | Awaitable[GrpcError]]
#: `call(request, transport)`: response bytes, `None` once a server stream is written, or
#: the failure to send as a status.
Call = Callable[[Any, TransportContext], Awaitable[bytes | RpcFailure | None]]

_NO_STACK: contextlib.nullcontext[None] = contextlib.nullcontext()


class _InvalidRequest(Exception):
    """The request bytes do not decode into the request model."""


@dataclass(frozen=True)
class Pipeline:
    """What the application adds around every call."""

    hooks: tuple[Hook, ...] = ()
    middlewares: tuple[Middleware, ...] = ()
    exception_handlers: Mapping[type[Exception], ExceptionHandler] = field(default_factory=dict)


class Invoker:
    """Runs one method: the transport awaits `call(request, transport_context)`."""

    def __init__(
        self,
        path: str,
        handler: Handler,
        request_codec: MessageCodec[Any],
        response_codec: MessageCodec[Any],
        plan: DependencyPlan,
        pipeline: Pipeline,
    ) -> None:
        self.path = path
        self.handler = handler
        self.request_codec = request_codec
        self.response_codec = response_codec
        self.response_model: type[BaseModel] = handler.method.response
        self.plan = plan
        self.pipeline = pipeline
        self.needs_context = bool(
            handler.context_parameter
            or plan.needs_context
            or pipeline.hooks
            or pipeline.middlewares
        )
        method = handler.method
        self.call: Call
        if method.server_streaming:
            self.call = self.stream_response
        elif method.client_streaming or self.needs_context or plan.nodes:
            self.call = self.unary_response
        else:
            self.call = self._plain_unary()

    def _plain_unary(self) -> Callable[[bytes, TransportContext], Awaitable[bytes | RpcFailure]]:
        """`unary_response` for the common case (request -> response, no context, hooks,
        middlewares or dependencies), without the branches: about 3 % more calls/s."""
        function = self.handler.function
        parameter = self.handler.request_parameter
        positional = _first_positional(function) == parameter
        decode, encode = self._decode, self._encode

        async def plain_unary(request: bytes, transport: TransportContext) -> bytes | RpcFailure:
            token = set_deadline(transport.time_remaining())
            try:
                message = decode(request)
                if positional:
                    response = await function(message)
                else:
                    response = await function(**{parameter: message})
                return encode(response)
            except Exception as e:  # every failure becomes a status
                return self._failure(await self._to_error(e))
            finally:
                _reset(token)

        return plain_unary

    async def unary_response(self, request: Any, transport: TransportContext) -> bytes | RpcFailure:
        token = set_deadline(transport.time_remaining())
        try:
            context = await self._prepare(transport)
            stack = contextlib.AsyncExitStack() if self.plan.needs_cleanup else _NO_STACK
            async with stack as exit_stack:
                response = await self._call(context, request, exit_stack, transport)
                return self._encode(response)
        except Exception as e:  # every failure becomes a status
            return self._failure(await self._to_error(e))
        finally:
            _reset(token)

    async def stream_response(self, request: Any, transport: TransportContext) -> RpcFailure | None:
        """A server stream: the responses are written by `_call`, inside the middlewares."""
        token = set_deadline(transport.time_remaining())
        try:
            context = await self._prepare(transport)
            stack = contextlib.AsyncExitStack() if self.plan.needs_cleanup else _NO_STACK
            async with stack as exit_stack:
                await self._call(context, request, exit_stack, transport)
            return None
        except Exception as e:  # every failure becomes a status
            return self._failure(await self._to_error(e))
        finally:
            _reset(token)

    async def _prepare(self, transport: TransportContext) -> Context | None:
        if not self.needs_context:
            return None
        context = Context(transport, self.path)
        for hook in self.pipeline.hooks:
            await hook(context)
        return context

    async def _call(
        self,
        context: Context | None,
        request: Any,
        stack: contextlib.AsyncExitStack | None,
        transport: TransportContext,
    ) -> Any:
        """Decode, then run the middlewares around the dependencies and the handler.

        Dependencies are resolved inside the middlewares, so a middleware that refuses a
        call opens nothing. A server stream is written inside them too: `call_next()`
        returns `None` once the last message is sent, so a middleware sees the whole
        stream (its duration, its errors).
        """
        handler = self.handler
        if handler.method.client_streaming:
            message: Any = self._decode_stream(request)
        else:
            message = self._decode(request)
        plan, encode = self.plan, self._encode

        async def call_handler() -> Any:
            kwargs = await plan.resolve(context, stack) if plan.nodes else {}
            kwargs[handler.request_parameter] = message
            if handler.context_parameter is not None:
                kwargs[handler.context_parameter] = context
            if not handler.method.server_streaming:
                return await handler.function(**kwargs)
            async with contextlib.aclosing(handler.function(**kwargs)) as responses:
                async for response in responses:
                    await transport.write(encode(response))
            return None

        call_next: CallNext = call_handler
        assert context is not None or not self.pipeline.middlewares
        for middleware in reversed(self.pipeline.middlewares):
            call_next = _bind(middleware, context, call_next)  # type: ignore[arg-type]
        return await call_next()

    def _decode(self, data: bytes) -> BaseModel:
        try:
            decoded: BaseModel = self.request_codec.decode(data)
        except (ValueError, TypeError, OverflowError) as e:
            raise _InvalidRequest(str(e)) from e
        return decoded

    async def _decode_stream(self, requests: AsyncIterable[bytes]) -> AsyncIterator[BaseModel]:
        async for data in requests:
            yield self._decode(data)

    def _encode(self, response: Any) -> bytes:
        if type(response) is not self.response_model:
            response = self.response_model.model_validate(response)
        return self.response_codec.encode(response)

    async def _to_error(self, error: Exception) -> GrpcError:
        if isinstance(error, GrpcError):
            return error
        if isinstance(error, _InvalidRequest):
            logger.debug("invalid request for %s: %s", self.path, error)
            return GrpcError(StatusCode.INVALID_ARGUMENT, "invalid request payload")
        for cls in type(error).__mro__:
            exception_handler = self.pipeline.exception_handlers.get(cls)
            if exception_handler is None:
                continue
            try:
                result = exception_handler(error)
                mapped = await result if inspect.isawaitable(result) else result
            except GrpcError as raised:
                return raised
            except Exception:
                logger.exception("exception handler failed in %s", self.path)
                break
            if isinstance(mapped, GrpcError):
                return mapped
            logger.error("exception handler for %s returned %r", cls.__name__, mapped)
            break
        logger.error("unhandled error in %s", self.path, exc_info=error)
        return GrpcError(StatusCode.INTERNAL, "internal server error")

    def _failure(self, error: GrpcError) -> RpcFailure:
        blob = None
        if error.details:
            try:
                blob = encode_details(error.code, error.message, error.details)
            except Exception:
                logger.exception("cannot encode the error details of %s", self.path)
        return RpcFailure(error.code, error.message, blob)


def _bind(middleware: Middleware, context: Context, call_next: CallNext) -> CallNext:
    async def next_step() -> Any:
        return await middleware(context, call_next)

    return next_step


def _first_positional(function: Callable[..., Any]) -> str | None:
    """Name of the first parameter, if it can be passed positionally."""
    for parameter in inspect.signature(function).parameters.values():
        positional = (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        return parameter.name if parameter.kind in positional else None
    return None


def _reset(token: Any) -> None:
    # An abandoned stream may be finalized outside the context it started in.
    with contextlib.suppress(ValueError):
        reset_deadline(token)
