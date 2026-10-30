"""The `grpc.aio` transport: a `grpc.aio` server whose method handlers run the invokers.

Bodies stay bytes (no serializer on the `grpc` side); an `RpcFailure` becomes `abort`.
"""

from collections.abc import Sequence
from typing import Any

import grpc
import grpc.aio

from fastgrpc3.api.config import Compression
from fastgrpc3.api.errors import DETAILS_TRAILER
from fastgrpc3.api.security import Insecure, Security, server_credentials
from fastgrpc3.runtime.dispatch import Invoker
from fastgrpc3.transport.base import RpcFailure

__all__ = ["GrpcAioTransport"]

ServicerContext = grpc.aio.ServicerContext[Any, Any]


class GrpcAioTransport:
    """A `TransportBackend` on `grpc.aio`."""

    def __init__(
        self,
        address: str,
        security: Security,
        *,
        interceptors: Sequence[grpc.aio.ServerInterceptor],
        options: Sequence[tuple[str, int | str]],
        maximum_concurrent_rpcs: int | None,
        compression: Compression | None,
    ) -> None:
        self.address = address
        self.security = security
        self._server = grpc.aio.server(
            interceptors=interceptors,
            options=options,
            maximum_concurrent_rpcs=maximum_concurrent_rpcs,
            compression=compression,
        )
        self._services: dict[str, dict[str, grpc.RpcMethodHandler[Any, Any]]] = {}

    def add_method(self, invoker: Invoker) -> None:
        service, name = invoker.path[1:].split("/")
        self._services.setdefault(service, {})[name] = _method_handler(invoker)

    async def start(self) -> int:
        for service, handlers in self._services.items():
            self._server.add_registered_method_handlers(service, handlers)
        if isinstance(self.security, Insecure):
            port = self._server.add_insecure_port(self.address)
        else:
            port = self._server.add_secure_port(self.address, server_credentials(self.security))
        if port == 0:
            raise RuntimeError(f"cannot bind {self.address}")
        await self._server.start()
        return port

    async def stop(self, grace: float | None) -> None:
        await self._server.stop(grace)

    async def wait_for_termination(self) -> None:
        await self._server.wait_for_termination()


def _method_handler(invoker: Invoker) -> "grpc.RpcMethodHandler[Any, Any]":
    call = invoker.call

    async def handle(request: Any, context: ServicerContext) -> Any:
        result = await call(request, context)
        if isinstance(result, RpcFailure):
            trailers = ((DETAILS_TRAILER, result.details),) if result.details else ()
            await context.abort(result.code, result.message, trailing_metadata=trailers)
        return result

    method = invoker.handler.method
    if method.server_streaming:
        if method.client_streaming:
            return grpc.stream_stream_rpc_method_handler(handle)
        return grpc.unary_stream_rpc_method_handler(handle)
    if method.client_streaming:
        return grpc.stream_unary_rpc_method_handler(handle)
    return grpc.unary_unary_rpc_method_handler(handle)
