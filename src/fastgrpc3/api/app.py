"""`GrpcApp`: services, hooks and settings; a server (`grpc.aio` or tonic) once started.

    app = GrpcApp(address="[::]:50051", tls=Insecure())
    app.add_service(service)
    asyncio.run(app.run())

Until `start()`, the app only holds configuration. `start()` freezes the contract (renders
and compiles the `.proto` of every service, numbers fields from `proto.lock`), builds one
invoker per method and starts the server.
"""

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import grpc
import grpc.aio
from pydantic import BaseModel

from fastgrpc3.api.config import (
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_MESSAGE_BYTES,
    DEFAULT_MAX_METADATA_BYTES,
    Compression,
    Keepalive,
    Limits,
    channel_options,
)
from fastgrpc3.api.deps import plan_dependencies
from fastgrpc3.api.errors import registered_details
from fastgrpc3.api.security import (
    MTLS,
    TLS,
    Insecure,
    Security,
    check_security,
)
from fastgrpc3.api.service import GrpcService
from fastgrpc3.ops.health import HealthService, ServingStatus
from fastgrpc3.ops.reflection import reflection_services
from fastgrpc3.runtime.dispatch import ExceptionHandler, Hook, Invoker, Middleware, Pipeline
from fastgrpc3.schema import Lock, Schema, SchemaRegistry
from fastgrpc3.schema.lock import Unlocked
from fastgrpc3.transport.base import TransportBackend
from fastgrpc3.transport.grpc_aio import GrpcAioTransport

if TYPE_CHECKING:
    from fastgrpc3.ops.docs import DocsServer

__all__ = ["GrpcApp"]

logger = logging.getLogger("fastgrpc3")

Lifespan = Callable[["GrpcApp"], contextlib.AbstractAsyncContextManager[None]]
TransportName = Literal["grpc_aio", "tonic"]
_COMPRESSIONS = {Compression.Gzip: "gzip", Compression.Deflate: "deflate"}


class GrpcApp:
    """A gRPC server application (see the module docstring)."""

    def __init__(
        self,
        title: str = "",
        *,
        address: str = "[::]:50051",
        tls: Security | None = None,
        compression: Compression | None = None,
        max_send_bytes: int | None = DEFAULT_MAX_MESSAGE_BYTES,
        max_recv_bytes: int | None = DEFAULT_MAX_MESSAGE_BYTES,
        max_metadata_bytes: int | None = DEFAULT_MAX_METADATA_BYTES,
        maximum_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
        keepalive: Keepalive | None = None,
        server_interceptors: Sequence[grpc.aio.ServerInterceptor] = (),
        lifespan: Lifespan | None = None,
        lock_file: str | Path | None = None,
        unlocked: Unlocked = "assign",
        sync_providers: Literal["loop", "threadpool"] = "loop",
        validate_requests: bool = True,
        options: Sequence[tuple[str, int | str]] = (),
        transport: TransportName = "grpc_aio",
    ) -> None:
        self.title = title
        #: `"grpc_aio"`, or `"tonic"` (experimental, needs `fastgrpc3-transport`); the
        #: `FASTGRPC_TRANSPORT` environment variable takes precedence.
        self.transport: TransportName = _check_transport(transport)
        self.address = address
        self.tls = check_security(tls)
        self.compression = compression
        self.limits = Limits(max_send_bytes, max_recv_bytes, max_metadata_bytes)
        if maximum_concurrent_rpcs is not None and maximum_concurrent_rpcs <= 0:
            raise ValueError("maximum_concurrent_rpcs must be positive or None")
        self.maximum_concurrent_rpcs = maximum_concurrent_rpcs
        self.keepalive = keepalive
        self.server_interceptors = list(server_interceptors)
        self.lifespan = lifespan
        self.lock_file = None if lock_file is None else Path(lock_file)
        self.unlocked: Unlocked = unlocked
        if sync_providers not in ("loop", "threadpool"):
            raise ValueError('sync_providers must be "loop" or "threadpool"')
        #: Where `def` dependency providers without `@blocking` / `@non_blocking` run.
        self.sync_providers = sync_providers
        #: False: requests are built without Pydantic validation (trusted clients only).
        self.validate_requests = validate_requests
        #: Extra raw `grpc` server options, e.g. `("grpc.so_reuseport", 1)`.
        self.options = list(options)
        self.health: HealthService | None = None
        #: The interactive docs, if `enable_docs` was called.
        self.docs: DocsServer | None = None
        self._reflection = False
        #: `{provider: replacement}`, read when the server starts.
        self.dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] = {}
        self.services: list[GrpcService] = []
        self.models: list[type[BaseModel]] = []
        self.hooks: list[Hook] = []
        self.middlewares: list[Middleware] = []
        self.exception_handlers: dict[type[Exception], ExceptionHandler] = {}
        #: The compiled contract, once started.
        self.schema: Schema | None = None
        self.bound_port: int | None = None
        self._server: TransportBackend | None = None
        self._lifespan: contextlib.AbstractAsyncContextManager[None] | None = None

    # -- configuration ---------------------------------------------------------------

    def add_service(self, service: GrpcService) -> None:
        """Serve a service."""
        self._check_not_started()
        if any(s.full_name == service.full_name for s in self.services):
            raise ValueError(f"service {service.full_name} added twice")
        self.services.append(service)

    include_service = add_service

    def add_model(self, model: type[BaseModel]) -> None:
        """Add a model no method uses to the contract (an error detail, for instance)."""
        self._check_not_started()
        self.models.append(model)

    def add_interceptor(self, hook: Hook) -> None:
        """Run `async hook(ctx)` before every call, before the request is decoded.

        Raising `GrpcError` refuses the call (authentication, quotas).
        """
        self._check_not_started()
        self.hooks.append(hook)

    def middleware(self, function: Middleware) -> Middleware:
        """Decorator: `async def mw(ctx, call_next)` around every handler, in order.

        `await call_next()` runs the rest of the chain and returns the handler's result (for
        a server stream, the async iterator of responses).
        """
        self._check_not_started()
        self.middlewares.append(function)
        return function

    def exception_handler[E: Exception](
        self, exception: type[E]
    ) -> Callable[[ExceptionHandler], ExceptionHandler]:
        """Decorator: map an exception type (and subclasses) to the `GrpcError` returned."""

        def register(function: ExceptionHandler) -> ExceptionHandler:
            self._check_not_started()
            self.exception_handlers[exception] = function
            return function

        return register

    def add_server_interceptor(self, interceptor: grpc.aio.ServerInterceptor) -> None:
        """Add a native `grpc.aio` interceptor (it sees raw methods and metadata)."""
        self._check_not_started()
        self.server_interceptors.append(interceptor)

    def enable_health(self) -> HealthService:
        """Serve `grpc.health.v1.Health`; services are SERVING once started, NOT_SERVING
        when stopping. Returns the `HealthService` to change statuses."""
        self._check_not_started()
        if self.health is None:
            self.health = HealthService()
            self.add_service(self.health.service)
        return self.health

    def enable_docs(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        gateway: bool = False,
        metadata: Sequence[str] = (),
        gateway_tls: TLS | MTLS | None = None,
        timeout: float = 30.0,
        version: str = "1.0.0",
        swagger_ui_url: str | None = None,
    ) -> "DocsServer":
        """Serve Swagger UI (`/docs`) and the OpenAPI of the services (`/openapi.json`) on
        their own HTTP port, with the server. Needs `fastgrpc3[docs]`.

        `gateway=True` lets "Try it out" call the RPCs: JSON in, the real gRPC call, JSON
        out; `metadata` are the headers forwarded as gRPC metadata (`x-user`...). It is a
        proxy to every RPC: keep it for development, or behind authentication. With TLS,
        `gateway_tls` is how the gateway connects to the app (`TLS(ca=...)`).
        """
        self._check_not_started()
        try:
            from fastgrpc3.ops.docs import SWAGGER_UI_URL, DocsServer, DocsSettings
        except ImportError as e:  # uvicorn is optional
            raise RuntimeError("interactive docs need: pip install 'fastgrpc3[docs]'") from e
        if gateway and gateway_tls is None and not isinstance(self.tls, Insecure):
            raise ValueError("a gateway to a TLS server needs gateway_tls=TLS(ca=...)")
        settings = DocsSettings(
            host=host,
            port=port,
            gateway=gateway,
            metadata=tuple(key.lower() for key in metadata),
            gateway_tls=gateway_tls,
            timeout=timeout,
            version=version,
            swagger_ui_url=swagger_ui_url or SWAGGER_UI_URL,
        )
        self.docs = DocsServer(self, settings)
        return self.docs

    def enable_reflection(self) -> None:
        """Serve gRPC server reflection (v1 and v1alpha), for `grpcurl` and similar tools."""
        self._check_not_started()
        if not self._reflection:
            self._reflection = True
            for service in reflection_services(lambda: self.schema):
                self.add_service(service)

    @property
    def schema_registry(self) -> SchemaRegistry:
        """The contract of the services and models (what `fastgrpc3 export` reads)."""
        registry = SchemaRegistry()
        for service in self.services:
            registry.add_service(service.service_def())
        for model in self.models:
            registry.add_model(model)
        return registry

    # -- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        """Compile the contract, build the handlers and start serving."""
        self._check_not_started()
        security = check_security(self.tls)
        if self.lifespan is not None:
            self._lifespan = self.lifespan(self)
            await self._lifespan.__aenter__()
        try:
            self.schema = self._build_schema()
            server = self._transport(security)
            self._register(server, self.schema)
            port = await server.start()
        except BaseException:
            await self._exit_lifespan()
            raise
        self._server = server
        self.bound_port = port
        if self.docs is not None:
            try:
                await self.docs.start()
            except BaseException:
                await self.stop(0)
                raise
        if self.health is not None:
            for service in self.services:
                self.health.set_serving(service.full_name)
            self.health.set_serving()
        logger.info("serving %d services on %s", len(self.services), self.address)

    async def stop(self, grace: float | None = None) -> None:
        """Stop the server, letting running calls finish for `grace` seconds."""
        server, self._server = self._server, None
        if server is not None and self.health is not None:
            self.health.set_all(ServingStatus.NOT_SERVING)
        try:
            if server is not None and self.docs is not None:
                await self.docs.stop()
            if server is not None:
                await server.stop(grace)
        finally:
            await self._exit_lifespan()

    async def wait_for_termination(self) -> None:
        """Wait until the server stops."""
        if self._server is not None:
            await self._server.wait_for_termination()

    async def run(self, grace: float = 5.0) -> None:
        """Start, serve until SIGINT or SIGTERM, then stop with `grace` seconds."""
        await self.start()
        stopping = asyncio.Event()
        with _signal_handlers(asyncio.get_running_loop(), stopping.set):
            try:
                await stopping.wait()
            finally:
                await self.stop(grace)

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncIterator["GrpcApp"]:
        """`async with app.serving():` start, then stop on exit."""
        await self.start()
        try:
            yield self
        finally:
            await self.stop(None)

    # -- internals -------------------------------------------------------------------

    def _check_not_started(self) -> None:
        if self._server is not None:
            raise RuntimeError("the server is already started")

    def _transport(self, security: Security) -> TransportBackend:
        transport = _check_transport(os.environ.get("FASTGRPC_TRANSPORT") or self.transport)
        if transport == "grpc_aio":
            return GrpcAioTransport(
                self.address,
                security,
                interceptors=self.server_interceptors,
                options=self._server_options(),
                maximum_concurrent_rpcs=self.maximum_concurrent_rpcs,
                compression=self.compression,
            )
        if self.server_interceptors:
            raise ValueError(
                "transport 'tonic' does not support native grpc.aio server interceptors: "
                "use fastgrpc3 middlewares or select grpc_aio"
            )
        reuse_port = False
        for name, value in self.options:  # `run --workers` shares the port
            if name != "grpc.so_reuseport":
                raise ValueError(f"transport 'tonic' does not support the grpc option {name!r}")
            reuse_port = bool(value)
        try:
            from fastgrpc3.transport.tonic import TonicTransport
        except ImportError as e:
            raise RuntimeError(
                "transport 'tonic' is not installed: pip install fastgrpc3-transport"
            ) from e
        return TonicTransport(
            self.address,
            security=security,
            maximum_concurrent_rpcs=self.maximum_concurrent_rpcs,
            limits=self.limits,
            compression=None if self.compression is None else _COMPRESSIONS.get(self.compression),
            keepalive=self.keepalive,
            reuse_port=reuse_port,
        )

    def _server_options(self) -> list[tuple[str, int | str]]:
        options = [*channel_options(self.limits, self.keepalive), *self.options]
        if not any(name == "grpc.so_reuseport" for name, _ in options):
            # gRPC shares ports by default: a second server on a busy port would start and
            # take part of the traffic. Fail instead; `run --workers` turns it back on.
            options.append(("grpc.so_reuseport", 0))
        return options

    def _build_schema(self) -> Schema:
        lock = Lock.load(self.lock_file) if self.lock_file is not None else None
        registry = self.schema_registry
        if self._reflection:
            # So that reflection clients (grpcurl) can decode error details.
            for model in registered_details():
                registry.add_model(model)
        schema = Schema.from_registry(registry, lock, self.unlocked)
        assigned = ", ".join(str(c) for c in schema.resolved.changes if c.automatic)
        if assigned and lock is None:
            # Numbered by declaration order, pinned nowhere: reordering fields breaks peers.
            logger.warning(
                "field numbers assigned at startup, from declaration order: %s "
                "(give them field(n), or pin them with lock_file= and `fastgrpc3 lock`)",
                assigned,
            )
        elif assigned:
            logger.warning(
                "field numbers not in %s, assigned at startup: %s (run `fastgrpc3 lock`)",
                self.lock_file,
                assigned,
            )
        return schema

    def _register(self, server: TransportBackend, schema: Schema) -> None:
        pipeline = Pipeline(
            tuple(self.hooks), tuple(self.middlewares), dict(self.exception_handlers)
        )
        for service in self.services:
            for name, handler in service.handlers.items():
                invoker = Invoker(
                    f"/{service.full_name}/{name}",
                    handler,
                    schema.codec(handler.method.request, validate=self.validate_requests),
                    schema.codec(handler.method.response),
                    plan_dependencies(
                        handler.dependencies,
                        self.dependency_overrides,
                        threadpool=self.sync_providers == "threadpool",
                    ),
                    pipeline,
                )
                server.add_method(invoker)

    async def _exit_lifespan(self) -> None:
        manager, self._lifespan = self._lifespan, None
        if manager is not None:
            await manager.__aexit__(None, None, None)


def _check_transport(name: str) -> TransportName:
    if name not in ("grpc_aio", "tonic"):
        raise ValueError(f'unknown transport {name!r}: "grpc_aio" or "tonic"')
    return name  # type: ignore[return-value]  # checked above


@contextlib.contextmanager
def _signal_handlers(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> Any:
    installed = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, callback)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):  # Windows, or not the main thread
            pass
    try:
        yield
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
