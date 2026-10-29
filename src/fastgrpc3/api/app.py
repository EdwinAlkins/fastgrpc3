"""`GrpcApp`: services, hooks and settings; a `grpc.aio` server once started.

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
import signal
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

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
from fastgrpc3.api.security import Insecure, Security, check_security, server_credentials
from fastgrpc3.api.service import GrpcService
from fastgrpc3.ops.health import HealthService, ServingStatus
from fastgrpc3.ops.reflection import reflection_services
from fastgrpc3.runtime.dispatch import ExceptionHandler, Hook, Invoker, Middleware, Pipeline
from fastgrpc3.schema import Lock, Schema, SchemaRegistry
from fastgrpc3.schema.lock import Unlocked

__all__ = ["GrpcApp"]

logger = logging.getLogger("fastgrpc3")

Lifespan = Callable[["GrpcApp"], contextlib.AbstractAsyncContextManager[None]]


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
        options: Sequence[tuple[str, int | str]] = (),
    ) -> None:
        self.title = title
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
        #: Extra raw `grpc` server options, e.g. `("grpc.so_reuseport", 1)`.
        self.options = list(options)
        self.health: HealthService | None = None
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
        self._server: grpc.aio.Server | None = None
        self._lifespan: contextlib.AbstractAsyncContextManager[None] | None = None

    # -- configuration ---------------------------------------------------------------

    def add_service(self, service: GrpcService) -> None:
        """Serve a service."""
        if any(s.full_name == service.full_name for s in self.services):
            raise ValueError(f"service {service.full_name} added twice")
        self.services.append(service)

    include_service = add_service

    def add_model(self, model: type[BaseModel]) -> None:
        """Add a model no method uses to the contract (an error detail, for instance)."""
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
            server = grpc.aio.server(
                interceptors=self.server_interceptors,
                options=self._server_options(),
                maximum_concurrent_rpcs=self.maximum_concurrent_rpcs,
                compression=self.compression,
            )
            self._register(server, self.schema)
            if isinstance(security, Insecure):
                port = server.add_insecure_port(self.address)
            else:
                port = server.add_secure_port(self.address, server_credentials(security))
            if port == 0:
                raise RuntimeError(f"cannot bind {self.address}")
            await server.start()
        except BaseException:
            await self._exit_lifespan()
            raise
        self._server = server
        self.bound_port = port
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
        invented = [str(c) for c in schema.resolved.changes if c.kind == "added"]
        if invented and lock is not None:
            logger.warning(
                "field numbers not in %s, assigned at startup: %s (run `fastgrpc3 lock`)",
                self.lock_file,
                ", ".join(invented),
            )
        return schema

    def _register(self, server: grpc.aio.Server, schema: Schema) -> None:
        pipeline = Pipeline(
            tuple(self.hooks), tuple(self.middlewares), dict(self.exception_handlers)
        )
        for service in self.services:
            handlers = {}
            for name, handler in service.handlers.items():
                invoker = Invoker(
                    f"/{service.full_name}/{name}",
                    handler,
                    schema.codec(handler.method.request),
                    schema.codec(handler.method.response),
                    plan_dependencies(handler.dependencies, self.dependency_overrides),
                    pipeline,
                )
                handlers[name] = invoker.rpc_method_handler()
            server.add_registered_method_handlers(service.full_name, handlers)

    async def _exit_lifespan(self) -> None:
        manager, self._lifespan = self._lifespan, None
        if manager is not None:
            await manager.__aexit__(None, None, None)


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
