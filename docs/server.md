# Server

## The application

```python
from fastgrpc3 import GrpcApp, TLS

app = GrpcApp(
    "orders",  # a title, for logs
    address="[::]:50051",
    tls=TLS(cert="server.pem", key="server.key"),
)
app.add_service(service)
```

| Argument | Default | |
|----------|---------|-|
| `tls` | — (required) | `TLS(cert, key)`, `MTLS(cert, key, ca)` or `Insecure()`; `None` is refused |
| `address` | `"[::]:50051"` | `host:port`; port `0` picks a free one (`app.bound_port` after start) |
| `max_send_bytes`, `max_recv_bytes` | 4 MiB | message size limits |
| `max_metadata_bytes` | 16 KiB | |
| `maximum_concurrent_rpcs` | 1024 | calls served at once; beyond, calls fail with `RESOURCE_EXHAUSTED` |
| `compression` | `None` | `Compression.Gzip`… |
| `keepalive` | `None` | `Keepalive(time_ms, timeout_ms, permit_without_calls)` |
| `lifespan` | `None` | `async` context manager factory run around the server |
| `lock_file`, `unlocked` | `None`, `"assign"` | `proto.lock` and what to do with unlocked fields (see [Contracts](contracts.md#field-numbers)) |
| `server_interceptors` | `()` | native `grpc.aio` interceptors |
| `validate_requests` | `True` | `False` builds requests without Pydantic validation: proto types are kept, the models' constraints and validators do not run; trusted clients only |
| `sync_providers` | `"loop"` | where `def` dependency providers run: `"loop"` or `"threadpool"` (see [Dependencies](#dependencies)) |
| `options` | `()` | raw `grpc` server options |
| `transport` | `Transport.GRPC_AIO` | or `Transport.TONIC`; see [Transport](#transport) |

PEM arguments take a path or the PEM bytes. With `MTLS`, clients must present a certificate
signed by `ca`; their identity is in `ctx.peer_identities()`.

Until `start()`, the app only holds configuration. `start()` compiles the contract of every
service, builds one handler per method, and starts the transport (`grpc.aio` by default).

## Handlers

```python
service = GrpcService("Orders", package="orders.v1")


@service.unary()  # request -> response
async def GetOrder(req: GetOrderRequest, ctx: Context) -> Order: ...


@service.unary_stream("Watch")  # request -> stream of responses
async def watch(req: GetOrderRequest) -> AsyncIterator[Order]:
    yield ...


@service.stream_unary()  # stream of requests -> response
async def Upload(lines: AsyncIterator[Line]) -> Summary: ...


@service.stream_stream()  # both ways
async def Chat(messages: AsyncIterator[Message]) -> AsyncIterator[Message]: ...
```

The RPC name is the decorator's argument, or the function's name. The signature is read
when the module is imported:

- the request is the parameter annotated with a model (`AsyncIterator[Model]` for a client
  stream);
- a parameter annotated `Context` receives the call's context (one at most);
- `Depends(...)` parameters are injected;
- the return annotation is the response model (`AsyncIterator[Model]` for a server stream,
  which must be an async generator).

Anything else (a parameter nothing can fill, a positional-only parameter, `*args` or
`**kwargs`, a wrong kind of function) raises `TypeError` at import. The decorated function is returned unchanged, so it can be called directly in
unit tests.

A handler may return the response model, a subclass instance, or a `dict` (validated into
the model). An instance of exactly the response model is not validated again.

## Context

| | |
|-|-|
| `ctx.method` | `"/orders.v1.Orders/GetOrder"` |
| `ctx.metadata` | request headers, a `Metadata` multimap (`get`, `get_all`) |
| `ctx.time_remaining()` | seconds before the deadline, or `None` |
| `ctx.peer()` | transport address of the caller |
| `ctx.peer_identities()`, `ctx.peer_identity()` | identities verified by mTLS |
| `ctx.auth_context()` | transport authentication properties |
| `await ctx.send_initial_metadata(md)` | response headers, before the first message |
| `ctx.set_trailing_metadata(md)` | response trailers |

`md` is a `Metadata`, a `dict` or pairs. Keys ending in `-bin` carry `bytes`.

## Dependencies

```python
from typing import Annotated
from fastgrpc3 import Context, Depends, blocking


def settings() -> Settings:
    return Settings()


@blocking  # synchronous driver: run in a thread
def legacy_lookup(config: Settings = Depends(settings)) -> Legacy:
    return Legacy.connect(config.legacy_url)


async def database(config: Settings = Depends(settings)) -> AsyncIterator[Database]:
    db = await Database.connect(config.url)
    try:
        yield db  # cleanup runs after the response
    finally:
        await db.close()


def current_user(ctx: Context) -> str:
    return str(ctx.metadata.get("x-user", "anonymous"))


@service.unary()
async def GetOrder(
    req: GetOrderRequest,
    db: Database = Depends(database),
    legacy: Legacy = Depends(legacy_lookup),
    user: Annotated[str, Depends(current_user)] = "",
) -> Order: ...
```

- A provider is a function, a coroutine function, a sync or async generator, a class (its
  `__init__` parameters are filled: `Depends(Repository)`), an instance whose `__call__` is
  any of these, or a `functools.partial`; its own parameters can be `Depends(...)` and
  `Context`.
- Within one call, a provider runs once and its value is shared
  (`Depends(provider, use_cache=False)` to opt out).
- Generator providers are cleaned up after the response, after the last message of a
  stream, and when the call fails or is cancelled. On failure, the exception is raised at
  their `yield`; a provider that catches it (to roll back, say) cannot swallow it: the call
  still fails, and the providers around it see the exception too.
- A `def` provider runs on the event loop: cheap, but while it blocks (a synchronous
  database driver, `requests`), no other call of the process moves. Mark it `@blocking` to
  run it in a thread instead, or set `GrpcApp(sync_providers="threadpool")` for every `def`
  provider, with `@non_blocking` to keep the quick ones on the loop. A thread round trip
  costs tens of µs per provider (see [Performance](performance.md)); a `def` provider that
  holds the loop more than 10 ms is reported once in the log.
- `app.dependency_overrides[provider] = replacement` swaps a provider; overrides are read
  when the app starts.
- The dependency graph is resolved at start: a cycle or a parameter nothing can fill fails
  then.

ruff's B008 flags `Depends(...)` in defaults; allow it as FastAPI projects do:

```toml
[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = ["fastgrpc3.Depends"]
```

## Hooks and middlewares

```python
async def authenticate(ctx: Context) -> None:
    if not valid(ctx.metadata.get("authorization")):
        raise GrpcError(StatusCode.UNAUTHENTICATED, "invalid token")


app.add_interceptor(authenticate)  # before every call, before the request is decoded


@app.middleware
async def timing(ctx: Context, call_next: CallNext) -> Any:
    started = time.perf_counter()
    try:
        return await call_next()  # the handler's result
    finally:
        log.info("%s took %.1f ms", ctx.method, (time.perf_counter() - started) * 1000)
```

Hooks run in order before the request is even decoded, so a refused call costs nothing.
Middlewares wrap the handler, the first added outermost. Dependencies are resolved inside
them, so a middleware that refuses a call opens no resource. For a server stream, the
whole stream is sent inside `call_next()`, which then returns `None`: a middleware sees
how long the stream lasted and the exception that ended it, if any. Hooks and middlewares
also run for the health and reflection services.

## Errors

```python
from fastgrpc3 import BadRequest, ErrorInfo, FieldViolation, GrpcError, StatusCode

raise GrpcError(
    StatusCode.INVALID_ARGUMENT,
    "invalid order",
    [BadRequest(field_violations=[FieldViolation(field="qty", description="must be > 0")])],
)
```

Details are the standard `google.rpc` messages (`ErrorInfo`, `RetryInfo`, `DebugInfo`,
`QuotaFailure`, `PreconditionFailure`, `BadRequest`, `RequestInfo`, `ResourceInfo`, `Help`,
`LocalizedMessage`), sent in the `grpc-status-details-bin` trailer: clients in any
language read them. A model of your own can be a detail too: decorate it with
`@register_detail` on both sides.

| What happens | Status sent |
|--------------|-------------|
| `raise GrpcError(code, message, details)` | that status |
| the request does not decode or validate | `INVALID_ARGUMENT "invalid request payload"` |
| an exception with a handler (below) | the `GrpcError` it returns |
| any other exception, or an invalid response | `INTERNAL "internal server error"`, logged with the method |

Nothing about an unexpected exception reaches the client. To map your own exceptions:

```python
@app.exception_handler(LookupError)
def not_found(error: Exception) -> GrpcError:
    return GrpcError(StatusCode.NOT_FOUND, str(error))
```

The handler covers subclasses; it can be `async`.

## Health and reflection

```python
health = app.enable_health()  # grpc.health.v1: Check, Watch, List
app.enable_reflection()  # grpc.reflection.v1 and v1alpha
```

Every service is `SERVING` once the server has started, and `NOT_SERVING` as soon as it
stops (before the grace period), so load balancers drain it. `health.set_not_serving(name)`,
`set_serving(name)` and `set_status(name, status)` change a status (`""` is the whole
server); `Check` of an unknown service answers `NOT_FOUND`. Reflection serves the
descriptor compiled at startup, so `grpcurl list` / `describe` / calls show exactly what
runs, and [grpcui](https://github.com/fullstorydev/grpcui) gives a web UI on top of it with
no setup (`grpcui -plaintext localhost:50051`): forms built from the messages, metadata,
streams.

## Interactive docs

```python
app.enable_docs(gateway=True, metadata=["x-user"])  # pip install 'fastgrpc3[docs]'
```

With the server, `enable_docs` serves on its own HTTP port (`127.0.0.1:8080` by default,
`host=` and `port=` to change it; `grpc.aio` speaks only gRPC):

- `/docs`: Swagger UI, one operation per RPC (`POST /<package>.<Service>/<Method>`), with
  the JSON Schema of the request and response models: constraints, defaults, descriptions,
  enum names;
- `/openapi.json`: the OpenAPI 3.1 document, generated from the services and models
  (`fastgrpc3 openapi app:app` writes it without starting anything).

`gateway=True` makes "Try it out" work: the JSON body is validated by the request model,
sent to the server over a gRPC channel of its own, so hooks, middlewares, dependencies and
exception handlers apply as for any client, and the response comes back as JSON. The
headers listed in `metadata` are forwarded as gRPC metadata. Streams are NDJSON, one JSON
object per line, both ways (client and bidirectional streams are sent as a batch). A
failed call answers a `Status` object with the HTTP status of its gRPC code:

```json
{"code": 5, "status": "NOT_FOUND", "message": "no book 0000000000",
 "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "BOOK_NOT_FOUND"}]}
```

In a response stream, an error after the first message arrives as a last line
`{"error": {...}}`. `INVALID_ARGUMENT` → 400, `UNAUTHENTICATED` → 401, `PERMISSION_DENIED` →
403, `NOT_FOUND` → 404, `ALREADY_EXISTS` → 409, `RESOURCE_EXHAUSTED` → 429, `INTERNAL` → 500,
`UNAVAILABLE` → 503 (as grpc-gateway maps them).

The gateway is a proxy to every RPC: it is off by default (the page then only documents)
and is meant for development, or behind authentication. With TLS, pass `gateway_tls=TLS(ca=...)`
for its connection to the server. The docs serve one port, so they refuse
`fastgrpc3 run --workers N`. Swagger UI's files come from a CDN; `swagger_ui_url=` points to
your own copy.

## Transport

The server underneath is `grpc.aio` by default. **tonic**, a Rust server
(`fastgrpc3-transport`, experimental), runs the same services, with the same handlers,
dependencies, hooks, middlewares and errors, 6 to 10 times faster per core under
concurrency (see [Performance](performance.md#on-tonic)).

```bash
pip install "fastgrpc3[tonic]"
```

It ships wheels for Linux (x86_64, aarch64), macOS (arm64, x86_64) and Windows (x86_64),
CPython 3.12 to 3.14; on Windows, `--workers` is not available with tonic.

Until it is published on PyPI, install it from GitHub, which builds it and needs a Rust
toolchain: `uv add "fastgrpc3-transport @ git+https://github.com/EdwinAlkins/fastgrpc3-transport"`.

Choose it without changing the application, in this order of precedence:

1. `fastgrpc3 run app:app --transport tonic`;
2. the environment variable `FASTGRPC_TRANSPORT=tonic`;
3. `GrpcApp(..., transport=Transport.TONIC)` (or `"tonic"`);
4. otherwise `grpc.aio`.

```python
from fastgrpc3 import Transport, available_transports

available_transports()  # [Transport.GRPC_AIO, Transport.TONIC] when installed
app.selected_transport  # what start() will use
app.transport_capabilities  # TransportCapabilities(..., native_grpc_aio_interceptors=False)
```

Checking what is installed does not load the Rust extension; `start()` does, and fails with
an explicit message if the transport is missing or its version does not match this
fastgrpc3. A setting the selected transport cannot honour also fails at `start()`, never
during a call. With tonic:

- native `grpc.aio` interceptors (`server_interceptors`) are refused: use
  [hooks and middlewares](#hooks-and-middlewares);
- raw `options` are refused, except `("grpc.so_reuseport", 1)`, which `--workers` uses;
- a unary call sends its response headers (`ctx.send_initial_metadata`) with its answer;
- `ctx.auth_context()` has the certificate properties but no TLS session details
  (`ssl_tls_version`...);
- request metadata over `max_metadata_bytes` closes the client's connection, where
  `grpc.aio` is more lenient;
- `Keepalive.permit_without_calls` has no effect.

The full comparison, with the test behind each line, is `docs/PARITY.md` in the
`fastgrpc3-transport` repository.

## Running

```python
asyncio.run(app.run())  # serve until SIGINT or SIGTERM, then stop with a grace period
```

or `fastgrpc3 run myapp.main:app`, or `async with app.serving(): ...` in your own code
(`start()` / `stop(grace)` underneath).

```bash
fastgrpc3 run myapp.main:app --address [::]:50051 --workers 4 --grace 10
```

`--workers N` starts N processes on the same port (`SO_REUSEPORT`, Linux spreads the
connections); one process uses one core, so this is how to use several. The parent
forwards SIGINT and SIGTERM, and stops every worker if one dies. Without `--workers`, a
port already in use makes the server fail to start.

`lifespan` runs code around the server's life:

```python
@contextlib.asynccontextmanager
async def lifespan(app: GrpcApp) -> AsyncIterator[None]:
    pool = await create_pool()
    yield
    await pool.close()


app = GrpcApp(tls=Insecure(), lifespan=lifespan)
```
