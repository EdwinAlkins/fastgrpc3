"""Client side: `Channel` (one connection, shared) and typed `GrpcClient` classes.

    class OrdersClient(GrpcClient):
        package = "orders.v1"
        service = "Orders"
        get_order = GrpcClient.unary("GetOrder", GetOrderRequest, Order)
        watch = GrpcClient.unary_stream("Watch", GetOrderRequest, Order)

    async with Channel("orders:50051", tls=TLS(ca="ca.pem")) as channel:
        orders = OrdersClient(channel)
        order = await orders.get_order(GetOrderRequest(order_number="A"))
        async for update in orders.watch(GetOrderRequest(order_number="A")): ...

The client encodes and decodes messages itself: a response that does not decode raises
`GrpcError(INTERNAL)` instead of silently giving `None`. A call made while serving a
request inherits what is left of that request's deadline.
"""

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from pathlib import Path
from typing import Any, ClassVar, Self, cast, overload

import grpc
import grpc.aio
from pydantic import BaseModel

from fastgrpc3.api.config import (
    DEFAULT_MAX_MESSAGE_BYTES,
    DEFAULT_MAX_METADATA_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    Compression,
    Keepalive,
    Limits,
    RetryPolicy,
    channel_options,
)
from fastgrpc3.api.errors import DETAILS_TRAILER, GrpcError, StatusCode, decode_details
from fastgrpc3.api.metadata import SENSITIVE_KEYS, MetadataLike, as_pairs
from fastgrpc3.api.security import (
    BearerToken,
    Insecure,
    Security,
    channel_credentials,
    check_security,
)
from fastgrpc3.runtime.deadline import remaining
from fastgrpc3.schema import Lock, MethodDef, Schema, SchemaRegistry, ServiceDef
from fastgrpc3.schema.codec import MessageCodec
from fastgrpc3.schema.registry import DEFAULT_PACKAGE

__all__ = ["Channel", "GrpcClient", "StreamStream", "StreamUnary", "Unary", "UnaryStream"]

Requests = AsyncIterable[Any] | Iterable[Any]


class Channel:
    """A connection to a server, shared by any number of clients."""

    def __init__(
        self,
        target: str,
        *,
        tls: Security | None = None,
        auth: BearerToken | None = None,
        default_timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        compression: Compression | None = None,
        max_send_bytes: int | None = DEFAULT_MAX_MESSAGE_BYTES,
        max_recv_bytes: int | None = DEFAULT_MAX_MESSAGE_BYTES,
        max_metadata_bytes: int | None = DEFAULT_MAX_METADATA_BYTES,
        retry: RetryPolicy | None = None,
        keepalive: Keepalive | None = None,
    ) -> None:
        security = check_security(tls)
        if isinstance(security, Insecure) and auth is not None:
            raise ValueError("credentials are refused on an insecure channel")
        if default_timeout is not None and default_timeout <= 0:
            raise ValueError("default_timeout must be positive or None")
        self.target = target
        self.secure = not isinstance(security, Insecure)
        self.default_timeout = default_timeout
        options = channel_options(
            Limits(max_send_bytes, max_recv_bytes, max_metadata_bytes), keepalive, retry
        )
        if isinstance(security, Insecure):
            self.grpc_channel = grpc.aio.insecure_channel(
                target, options=options, compression=compression
            )
            return
        credentials = channel_credentials(security)
        if auth is not None:
            credentials = grpc.composite_channel_credentials(
                credentials, grpc.access_token_call_credentials(auth.token)
            )
        self.grpc_channel = grpc.aio.secure_channel(
            target, credentials, options=options, compression=compression
        )

    async def close(self, grace: float | None = None) -> None:
        """Close the connection (after `grace` seconds for running calls)."""
        await self.grpc_channel.close(grace)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


class GrpcClient:
    """Base of typed clients; subclasses set `package`, `service` and the methods.

    The contract is compiled once per client class, from the method models (and from
    `lock_file`, the `proto.lock` of the server, if the models leave numbers to it).
    """

    package: ClassVar[str] = DEFAULT_PACKAGE
    service: ClassVar[str] = ""
    lock_file: ClassVar[str | Path | None] = None
    _schema: ClassVar[Schema | None] = None

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self._callables: dict[str, Any] = {}

    @classmethod
    def service_def(cls) -> ServiceDef:
        """The contract of the client's methods."""
        methods = [value.method for value in _class_attributes(cls) if isinstance(value, _Method)]
        return ServiceDef(cls.service or cls.__name__, cls.package, tuple(methods))

    @classmethod
    def schema(cls) -> Schema:
        """The compiled contract (built on first use)."""
        if "_schema" not in vars(cls) or cls._schema is None:
            registry = SchemaRegistry()
            registry.add_service(cls.service_def())
            lock = Lock.load(Path(cls.lock_file)) if cls.lock_file is not None else None
            cls._schema = Schema.from_registry(registry, lock)
        return cls._schema

    @staticmethod
    def unary[Req: BaseModel, Resp: BaseModel](
        name: str, request: type[Req], response: type[Resp]
    ) -> "Unary[Req, Resp]":
        """A request -> response method."""
        return Unary(MethodDef(name, request, response))

    @staticmethod
    def unary_stream[Req: BaseModel, Resp: BaseModel](
        name: str, request: type[Req], response: type[Resp]
    ) -> "UnaryStream[Req, Resp]":
        """A request -> stream of responses method."""
        return UnaryStream(MethodDef(name, request, response, server_streaming=True))

    @staticmethod
    def stream_unary[Req: BaseModel, Resp: BaseModel](
        name: str, request: type[Req], response: type[Resp]
    ) -> "StreamUnary[Req, Resp]":
        """A stream of requests -> response method."""
        return StreamUnary(MethodDef(name, request, response, client_streaming=True))

    @staticmethod
    def stream_stream[Req: BaseModel, Resp: BaseModel](
        name: str, request: type[Req], response: type[Resp]
    ) -> "StreamStream[Req, Resp]":
        """A bidirectional streaming method."""
        return StreamStream(MethodDef(name, request, response, True, True))


class _Method[Req: BaseModel, Resp: BaseModel]:
    """A method declared on a `GrpcClient` class; bound to an instance, it is callable."""

    def __init__(self, method: MethodDef) -> None:
        self.method = method

    def _prepare(
        self, client: GrpcClient, timeout: float | None, metadata: MetadataLike | None
    ) -> tuple[Any, MessageCodec[Any], MessageCodec[Any], dict[str, Any]]:
        schema = type(client).schema()
        multicallable = client._callables.get(self.method.name)
        if multicallable is None:
            path = f"/{client.package}.{client.service or type(client).__name__}/{self.method.name}"
            factory = {
                (False, False): client.channel.grpc_channel.unary_unary,
                (False, True): client.channel.grpc_channel.unary_stream,
                (True, False): client.channel.grpc_channel.stream_unary,
                (True, True): client.channel.grpc_channel.stream_stream,
            }[(self.method.client_streaming, self.method.server_streaming)]
            multicallable = factory(path)
            client._callables[self.method.name] = multicallable
        pairs = as_pairs(metadata)
        if not client.channel.secure and any(key in SENSITIVE_KEYS for key, _ in pairs):
            raise ValueError("credentials are refused on an insecure channel")
        options = {"timeout": _timeout(client.channel, timeout), "metadata": pairs or None}
        request_codec = schema.codec(self.method.request)
        return multicallable, request_codec, schema.codec(self.method.response), options


class Unary[Req: BaseModel, Resp: BaseModel](_Method[Req, Resp]):
    @overload
    def __get__(self, client: None, owner: type) -> Self: ...
    @overload
    def __get__(self, client: GrpcClient, owner: type) -> "_UnaryCall[Req, Resp]": ...
    def __get__(self, client: GrpcClient | None, owner: type) -> Any:
        return self if client is None else _UnaryCall(self, client)


class UnaryStream[Req: BaseModel, Resp: BaseModel](_Method[Req, Resp]):
    @overload
    def __get__(self, client: None, owner: type) -> Self: ...
    @overload
    def __get__(self, client: GrpcClient, owner: type) -> "_UnaryStreamCall[Req, Resp]": ...
    def __get__(self, client: GrpcClient | None, owner: type) -> Any:
        return self if client is None else _UnaryStreamCall(self, client)


class StreamUnary[Req: BaseModel, Resp: BaseModel](_Method[Req, Resp]):
    @overload
    def __get__(self, client: None, owner: type) -> Self: ...
    @overload
    def __get__(self, client: GrpcClient, owner: type) -> "_StreamUnaryCall[Req, Resp]": ...
    def __get__(self, client: GrpcClient | None, owner: type) -> Any:
        return self if client is None else _StreamUnaryCall(self, client)


class StreamStream[Req: BaseModel, Resp: BaseModel](_Method[Req, Resp]):
    @overload
    def __get__(self, client: None, owner: type) -> Self: ...
    @overload
    def __get__(self, client: GrpcClient, owner: type) -> "_StreamStreamCall[Req, Resp]": ...
    def __get__(self, client: GrpcClient | None, owner: type) -> Any:
        return self if client is None else _StreamStreamCall(self, client)


class _Call[Req: BaseModel, Resp: BaseModel]:
    def __init__(self, method: _Method[Req, Resp], client: GrpcClient) -> None:
        self.method = method
        self.client = client


class _UnaryCall[Req: BaseModel, Resp: BaseModel](_Call[Req, Resp]):
    async def __call__(
        self, request: Req, *, timeout: float | None = None, metadata: MetadataLike | None = None
    ) -> Resp:
        call, request_codec, response_codec, options = self.method._prepare(
            self.client, timeout, metadata
        )
        data = request_codec.encode(_as_model(request, self.method.method.request))
        try:
            raw = await call(data, **options)
        except grpc.aio.AioRpcError as e:
            raise to_grpc_error(e) from e
        return cast(Resp, _decode(response_codec, raw))


class _UnaryStreamCall[Req: BaseModel, Resp: BaseModel](_Call[Req, Resp]):
    async def __call__(
        self, request: Req, *, timeout: float | None = None, metadata: MetadataLike | None = None
    ) -> AsyncIterator[Resp]:
        call, request_codec, response_codec, options = self.method._prepare(
            self.client, timeout, metadata
        )
        data = request_codec.encode(_as_model(request, self.method.method.request))
        try:
            async for raw in call(data, **options):
                yield cast(Resp, _decode(response_codec, raw))
        except grpc.aio.AioRpcError as e:
            raise to_grpc_error(e) from e


class _StreamUnaryCall[Req: BaseModel, Resp: BaseModel](_Call[Req, Resp]):
    async def __call__(
        self,
        requests: AsyncIterable[Req] | Iterable[Req],
        *,
        timeout: float | None = None,
        metadata: MetadataLike | None = None,
    ) -> Resp:
        call, request_codec, response_codec, options = self.method._prepare(
            self.client, timeout, metadata
        )
        encoded = _encode_stream(requests, request_codec, self.method.method.request)
        try:
            raw = await call(encoded, **options)
        except grpc.aio.AioRpcError as e:
            raise to_grpc_error(e) from e
        return cast(Resp, _decode(response_codec, raw))


class _StreamStreamCall[Req: BaseModel, Resp: BaseModel](_Call[Req, Resp]):
    async def __call__(
        self,
        requests: AsyncIterable[Req] | Iterable[Req],
        *,
        timeout: float | None = None,
        metadata: MetadataLike | None = None,
    ) -> AsyncIterator[Resp]:
        call, request_codec, response_codec, options = self.method._prepare(
            self.client, timeout, metadata
        )
        encoded = _encode_stream(requests, request_codec, self.method.method.request)
        try:
            async for raw in call(encoded, **options):
                yield cast(Resp, _decode(response_codec, raw))
        except grpc.aio.AioRpcError as e:
            raise to_grpc_error(e) from e


def to_grpc_error(error: grpc.aio.AioRpcError) -> GrpcError:
    """A `GrpcError` from a failed call, with the details of its trailer."""
    details: list[BaseModel] = []
    for key, value in error.trailing_metadata() or ():
        if key == DETAILS_TRAILER and isinstance(value, bytes):
            try:
                _, _, details = decode_details(value)
            except (ValueError, TypeError):
                # A broken details trailer must not hide the status itself.
                details = []
    return GrpcError(error.code(), error.details() or "", details)


def _timeout(channel: Channel, timeout: float | None) -> float | None:
    chosen = timeout if timeout is not None else channel.default_timeout
    budget = remaining()
    if budget is None:
        return chosen
    return budget if chosen is None else min(chosen, budget)


def _as_model[M: BaseModel](value: Any, model: type[M]) -> M:
    return value if isinstance(value, model) else model.model_validate(value)


async def _encode_stream(
    requests: Requests, codec: MessageCodec[Any], model: type[BaseModel]
) -> AsyncIterator[bytes]:
    if isinstance(requests, AsyncIterable):
        async for request in requests:
            yield codec.encode(_as_model(request, model))
    else:
        for request in requests:
            yield codec.encode(_as_model(request, model))


def _decode[M: BaseModel](codec: MessageCodec[M], data: bytes) -> M:
    try:
        return codec.decode(data)
    except (ValueError, TypeError, OverflowError) as e:
        raise GrpcError(StatusCode.INTERNAL, "invalid response payload") from e


def _class_attributes(cls: type) -> list[Any]:
    seen: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        seen.update(vars(klass))
    return list(seen.values())
