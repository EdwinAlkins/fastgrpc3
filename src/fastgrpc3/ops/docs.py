"""Interactive documentation (`app.enable_docs`): Swagger UI over the app's OpenAPI, on an
HTTP port of its own, and an optional JSON -> gRPC gateway for "Try it out".

`grpc.aio` serves only gRPC, so the docs run their own small HTTP server (uvicorn, extra
`fastgrpc3[docs]`) in the app's event loop. Routes:

- `GET /docs`: Swagger UI (its files come from a CDN, `swagger_ui_url` to change it);
- `GET /openapi.json`: the OpenAPI document (`fastgrpc3.ops.openapi`);
- `POST /<package>.<Service>/<Method>`: the gateway, if enabled. The JSON body is validated
  by the request model, sent to the app over a gRPC channel of its own (hooks, middlewares,
  authentication and errors apply as for any client), and the response comes back as JSON;
  streams are NDJSON. Only the headers listed in `metadata` are forwarded, as gRPC metadata.

The gateway is a proxy to every RPC: it is off by default and listens on 127.0.0.1 unless
told otherwise.
"""

import asyncio
import base64
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import grpc
import grpc.aio
import uvicorn
from pydantic import BaseModel, ValidationError

from fastgrpc3.api.client import to_grpc_error
from fastgrpc3.api.errors import (
    BadRequest,
    FieldViolation,
    GrpcError,
    StatusCode,
    detail_type_url,
)
from fastgrpc3.api.security import MTLS, TLS, Insecure, channel_credentials
from fastgrpc3.ops.openapi import HTTP_STATUS, NDJSON, documented_services, openapi
from fastgrpc3.schema import MethodDef
from fastgrpc3.schema.codec import MessageCodec
from fastgrpc3.types import AnyMessage

if TYPE_CHECKING:
    from fastgrpc3.api.app import GrpcApp

__all__ = ["DocsServer", "DocsSettings"]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"


@dataclass(frozen=True, slots=True)
class DocsSettings:
    """What `app.enable_docs(...)` asked for."""

    host: str = "127.0.0.1"
    port: int = 8080
    gateway: bool = False
    metadata: tuple[str, ...] = ()
    gateway_tls: TLS | MTLS | None = None
    timeout: float = 30.0
    version: str = "1.0.0"
    swagger_ui_url: str = SWAGGER_UI_URL


@dataclass(frozen=True, slots=True)
class _Route:
    method: MethodDef
    request: MessageCodec[Any]
    response: MessageCodec[Any]


class _HttpError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("message", ""))
        self.status = status
        self.body = body


class DocsServer:
    """The HTTP side of `app.enable_docs`: started and stopped with the app."""

    def __init__(self, app: "GrpcApp", settings: DocsSettings) -> None:
        self.app = app
        self.settings = settings
        #: The port the docs listen on, once started.
        self.port: int | None = None
        self._routes: dict[str, _Route] = {}
        self._openapi = b""
        self._page = b""
        self._channel: grpc.aio.Channel | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def openapi(self) -> dict[str, Any]:
        """The OpenAPI document of the app's services."""
        settings = self.settings
        return openapi(
            self.app.services,
            title=self.app.title,
            version=settings.version,
            metadata=settings.metadata,
        )

    async def start(self) -> None:
        """Serve the docs (the app is started: its schema and port are known)."""
        settings, schema = self.settings, self.app.schema
        assert schema is not None and self.app.bound_port is not None
        self._openapi = json.dumps(self.openapi()).encode()
        self._page = _swagger_page(self.app.title, settings).encode()
        if settings.gateway:
            for service in documented_services(self.app.services):
                for method in service.service_def().methods:
                    route = _Route(
                        method, schema.codec(method.request), schema.codec(method.response)
                    )
                    self._routes[f"/{service.full_name}/{method.name}"] = route
            self._channel = self._open_channel()
        sock = _listen(settings.host, settings.port)
        config = uvicorn.Config(
            cast(Any, self),
            log_level="warning",
            lifespan="off",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
        server = _Server(config)
        self._server = server
        self._task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if self._task.done():
                await self._task
                raise RuntimeError("the docs server stopped while starting")
            await asyncio.sleep(0.01)
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        """Stop serving the docs and close the gateway's channel."""
        server, task, channel = self._server, self._task, self._channel
        self._server = self._task = self._channel = None
        if server is not None and task is not None:
            server.should_exit = True
            await task
        if channel is not None:
            await channel.close()

    def _open_channel(self) -> grpc.aio.Channel:
        app = self.app
        host = app.address.rpartition(":")[0]
        if host in ("", "[::]", "0.0.0.0"):
            host = "127.0.0.1"
        target = f"{host}:{app.bound_port}"
        if isinstance(app.tls, Insecure):
            return grpc.aio.insecure_channel(target)
        assert self.settings.gateway_tls is not None  # checked by enable_docs
        return grpc.aio.secure_channel(target, channel_credentials(self.settings.gateway_tls))

    # -- ASGI -----------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        path, verb = scope["path"], scope["method"]
        try:
            if verb == "GET" and path == "/docs":
                await _respond(send, 200, "text/html; charset=utf-8", self._page)
            elif verb == "GET" and path == "/openapi.json":
                await _respond(send, 200, "application/json", self._openapi)
            elif verb == "POST" and self.settings.gateway and path in self._routes:
                await self._call(self._routes[path], scope, receive, send)
            elif verb == "POST" and not self.settings.gateway:
                raise _http_error(StatusCode.UNIMPLEMENTED, "the docs gateway is disabled")
            else:
                raise _http_error(StatusCode.NOT_FOUND, f"no route {verb} {path}")
        except _HttpError as e:
            await _respond(send, e.status, "application/json", json.dumps(e.body).encode())

    async def _call(self, route: _Route, scope: Scope, receive: Receive, send: Send) -> None:
        assert self._channel is not None
        method = route.method
        body = await _read_body(receive, self.app.limits.max_recv_bytes)
        headers = {
            key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]
        }
        metadata = [(key, headers[key]) for key in self.settings.metadata if key in headers]
        timeout = self.settings.timeout
        path = scope["path"]
        message: Any
        if method.client_streaming:
            lines = [line for line in body.splitlines() if line.strip()]
            message = iter([route.request.encode(_parse(method.request, line)) for line in lines])
        else:
            message = route.request.encode(_parse(method.request, body))
        # Bodies stay bytes: no serializer on the `grpc` side, the codecs are ours.
        channel: Any = self._channel
        kind = {
            (False, False): channel.unary_unary,
            (False, True): channel.unary_stream,
            (True, False): channel.stream_unary,
            (True, True): channel.stream_stream,
        }[(method.client_streaming, method.server_streaming)]
        call = kind(path)(message, metadata=metadata, timeout=timeout)
        try:
            if method.server_streaming:
                await _stream(send, route.response, call)
            else:
                reply = _dump(route.response.decode(await call))
                await _respond(send, 200, "application/json", reply)
        except grpc.aio.AioRpcError as e:
            error = to_grpc_error(e)
            raise _HttpError(HTTP_STATUS.get(error.code, 500), _status(error)) from e


class _Server(uvicorn.Server):
    """uvicorn without its signal handlers: the app handles SIGINT and SIGTERM."""

    @contextlib.contextmanager
    def capture_signals(self) -> Any:
        yield


def _listen(host: str, port: int) -> socket.socket:
    """The docs' listening socket, bound here so that a busy port is a plain `OSError`."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host.strip("[]"), port), family=family)


async def _read_body(receive: Receive, limit: int | None) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        chunk = message.get("body", b"")
        size += len(chunk)
        if limit is not None and size > limit:
            raise _http_error(StatusCode.RESOURCE_EXHAUSTED, "request body too large", 413)
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _parse(model: type[BaseModel], data: bytes) -> BaseModel:
    """The request model from JSON, or a 400 with the violations as `BadRequest`."""
    try:
        return model.model_validate_json(data or b"{}")
    except ValidationError as e:
        violations = [
            FieldViolation(
                field=".".join(str(part) for part in error["loc"]), description=error["msg"]
            )
            for error in e.errors()
        ]
        detail = BadRequest(field_violations=violations)
        error = GrpcError(StatusCode.INVALID_ARGUMENT, "invalid request JSON", [detail])
        raise _HttpError(400, _status(error)) from e


async def _stream(send: Send, codec: MessageCodec[Any], call: Any) -> None:
    """NDJSON responses; an error before the first one is a plain error response, after it a
    last line `{"error": Status}` (the HTTP status is already sent)."""
    responses: AsyncIterator[bytes] = call.__aiter__()
    try:
        first = await anext(responses)
    except StopAsyncIteration:
        await _respond(send, 200, NDJSON, b"")
        return
    headers = [(b"content-type", NDJSON.encode())]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    line = _dump(codec.decode(first)) + b"\n"
    await send({"type": "http.response.body", "body": line, "more_body": True})
    try:
        async for raw in responses:
            line = _dump(codec.decode(raw)) + b"\n"
            await send({"type": "http.response.body", "body": line, "more_body": True})
    except grpc.aio.AioRpcError as e:
        line = json.dumps({"error": _status(to_grpc_error(e))}).encode() + b"\n"
        await send({"type": "http.response.body", "body": line, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _dump(message: BaseModel) -> bytes:
    return message.model_dump_json(by_alias=True).encode()


def _status(error: GrpcError) -> dict[str, Any]:
    """`google.rpc.Status` as JSON, details with their `@type`."""
    details = []
    for detail in error.details:
        if isinstance(detail, AnyMessage):
            value = base64.b64encode(detail.value).decode()
            details.append({"@type": detail.type_url, "value": value})
        else:
            dumped = detail.model_dump(mode="json", by_alias=True)
            details.append({"@type": detail_type_url(detail), **dumped})
    code = error.code
    return {
        "code": code.value[0],
        "status": code.name,
        "message": error.message,
        "details": details,
    }


def _http_error(code: StatusCode, message: str, status: int | None = None) -> _HttpError:
    error = GrpcError(code, message)
    return _HttpError(status or HTTP_STATUS.get(code, 500), _status(error))


async def _respond(send: Send, status: int, content_type: str, body: bytes) -> None:
    headers = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _swagger_page(title: str, settings: DocsSettings) -> str:
    base = settings.swagger_ui_url.rstrip("/")
    submit: Sequence[str] = ["post"] if settings.gateway else []
    options = {"url": "openapi.json", "dom_id": "#swagger-ui", "supportedSubmitMethods": submit}
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{_escape(title or "gRPC services")} - docs</title>
<link rel="stylesheet" href="{base}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{base}/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({json.dumps(options)});</script>
</body>
</html>
"""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
