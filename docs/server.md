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
| `options` | `()` | raw `grpc` server options |

PEM arguments take a path or the PEM bytes. With `MTLS`, clients must present a certificate
signed by `ca`; their identity is in `ctx.peer_identities()`.

Until `start()`, the app only holds configuration. `start()` compiles the contract of every
service, builds one handler per method, and starts `grpc.aio`.

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
from fastgrpc3 import Context, Depends


def settings() -> Settings:
    return Settings()


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
    user: Annotated[str, Depends(current_user)] = "",
) -> Order: ...
```

- A provider is a function, a coroutine function, or a sync or async generator; its own
  parameters can be `Depends(...)` and `Context`.
- Within one call, a provider runs once and its value is shared
  (`Depends(provider, use_cache=False)` to opt out).
- Generator providers are cleaned up after the response, after the last message of a
  stream, and when the call fails or is cancelled. On failure, the exception is raised at
  their `yield`; a provider that catches it (to roll back, say) cannot swallow it: the call
  still fails, and the providers around it see the exception too.
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
Middlewares wrap the handler, the first added outermost; for a server stream,
`call_next()` returns the async iterator of responses. Hooks and middlewares also run for
the health and reflection services.

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
runs.

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
