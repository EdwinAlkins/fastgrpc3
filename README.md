# fastgrpc3

**gRPC for Python, the FastAPI way — and up to 14× faster per core.**

[![PyPI](https://img.shields.io/pypi/v/fastgrpc3)](https://pypi.org/project/fastgrpc3/)
[![Python](https://img.shields.io/pypi/pyversions/fastgrpc3)](https://pypi.org/project/fastgrpc3/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Write gRPC services as plain `async` functions with Pydantic models. fastgrpc3 gives you
dependency injection, typed clients, rich errors, health checking, reflection and
interactive docs — and protobuf encoded and decoded in Rust. Add the Rust transport, and the
same app serves **62,000 calls per second on a single core**.

```python
from pydantic import BaseModel
from fastgrpc3 import GrpcApp, GrpcService, Insecure


class Hello(BaseModel):
    name: str


class Reply(BaseModel):
    text: str


service = GrpcService("Greeter", package="hello.v1")


@service.unary()
async def SayHello(req: Hello) -> Reply:
    return Reply(text=f"Hello, {req.name}!")


app = GrpcApp(tls=Insecure())
app.add_service(service)
```

```bash
pip install "fastgrpc3[tonic]"
fastgrpc3 run hello:app --transport tonic
```

## Why fastgrpc3

- **Fast where it counts.** On one core, an app on the tonic transport answers 14× more
  calls than on `grpc.aio`, with 17× less CPU per call and a p99 latency 8× lower — without
  changing a line of the handlers. On `grpc.aio`, the framework costs about 3 % over
  hand-written stubs.
- **Pydantic in, Pydantic out.** Requests and responses are Pydantic models, validated on
  the way in; protobuf runs in Rust through
  [protoruf](https://github.com/EdwinAlkins/protoruf), with no `protoc` and no `_pb2` files.
- **Two ways to declare a contract.** Write Pydantic models and get the `.proto`, with field
  numbers pinned in `proto.lock` and breaking changes caught in CI; or start from `.proto`
  files and generate models, services and typed clients that pass `mypy --strict`.
- **Everything a service needs.** The four kinds of call, `Depends(...)` with `yield`
  cleanup, hooks, middlewares, exception handlers, deadlines propagated to downstream calls,
  standard `google.rpc` error details, `TestClient`.
- **Standard gRPC.** Real protobuf on the wire: clients in any language can call your
  service. Health checking (Kubernetes, `grpc-health-probe`), server reflection (`grpcurl`),
  Swagger UI with a JSON gateway to try the RPCs from the browser.
- **Secure by default.** `TLS(...)`, `MTLS(...)` or `Insecure()`, always explicit; client
  identities from mTLS certificates.

## Performance

One CPU core, unary calls, 64 concurrent clients:

| | calls/s | CPU per call | p99 |
|---|--:|--:|--:|
| **fastgrpc3 on tonic** | **62,240** | **13 µs** | **2.0 ms** |
| fastgrpc3 on `grpc.aio` | 4,430 | 226 µs | 15.9 ms |
| `grpc.aio` + generated stubs, no framework | 4,571 | 219 µs | 18.0 ms |
| fastgrpc2 | 4,108 | 244 µs | 19.4 ms |

The tonic transport ([fastgrpc3-transport](https://github.com/EdwinAlkins/fastgrpc3-transport))
runs HTTP/2, gRPC framing, TLS and flow control in Rust and hands the calls to asyncio by
batches; wheels for Linux, macOS and Windows. Medium and large messages, latency, several
cores, dependencies, codec and method: [docs/performance.md](docs/performance.md).

## Install

```bash
pip install fastgrpc3              # grpc.aio transport
pip install "fastgrpc3[tonic]"     # + the Rust transport (experimental)
pip install "fastgrpc3[docs]"      # + Swagger UI and the JSON gateway
```

Python 3.12, 3.13 and 3.14. fastgrpc3 is at version 0.1: the API may still change.
A complete example, a bookstore service with its client and tests, is in
[`examples/`](examples/README.md).

## Server and client

```python
import asyncio
from collections.abc import AsyncIterator

from pydantic import BaseModel
from fastgrpc3 import Context, Depends, GrpcApp, GrpcError, GrpcService, Insecure, StatusCode


class GetOrder(BaseModel):
    order_number: str


class Order(BaseModel):
    order_number: str
    paid: bool = False


service = GrpcService("Orders", package="orders.v1")


def get_repo() -> dict[str, Order]:
    return {"A": Order(order_number="A", paid=True)}


@service.unary()
async def Get(req: GetOrder, ctx: Context, repo: dict[str, Order] = Depends(get_repo)) -> Order:
    if req.order_number not in repo:
        raise GrpcError(StatusCode.NOT_FOUND, "no such order")
    return repo[req.order_number]


@service.unary_stream()
async def Watch(req: GetOrder) -> AsyncIterator[Order]:
    yield Order(order_number=req.order_number)


app = GrpcApp(address="[::]:50051", tls=Insecure())  # or TLS(cert=..., key=...)
app.add_service(service)
app.enable_health()  # grpc.health.v1: Check, Watch, List (grpc-health-probe, Kubernetes)
app.enable_reflection()  # grpc.reflection v1 and v1alpha (grpcurl list / describe / call)
app.enable_docs(gateway=True)  # Swagger UI on 127.0.0.1:8080/docs (fastgrpc3[docs])

if __name__ == "__main__":
    asyncio.run(app.run())  # stops cleanly on SIGINT / SIGTERM
```

```python
from fastgrpc3 import Channel, GrpcClient, Insecure


class OrdersClient(GrpcClient):
    package = "orders.v1"
    service = "Orders"
    get = GrpcClient.unary("Get", GetOrder, Order)
    watch = GrpcClient.unary_stream("Watch", GetOrder, Order)


async def main() -> None:
    async with Channel("localhost:50051", tls=Insecure()) as channel:
        orders = OrdersClient(channel)
        order = await orders.get(GetOrder(order_number="A"))
        async for update in orders.watch(GetOrder(order_number="A")):
            print(update)
```

Also: dependencies with sub-dependencies, per-call cache and `yield` cleanup;
`app.add_interceptor` (pre-call hooks), `@app.middleware` (around the handler) and
`@app.exception_handler`; rich errors with the standard `google.rpc` details (`ErrorInfo`,
`BadRequest`…); deadline propagation to calls made from a handler; `TLS` / `MTLS` /
`Insecure` always explicit; `fastgrpc3.testing.TestClient` for tests.

```bash
fastgrpc3 run myapp.main:app --address [::]:50051 --workers 4   # one process per core
```

Workers share the port with `SO_REUSEPORT` (Linux); otherwise a busy port makes the server
fail to start instead of silently sharing it.

## Code first: Pydantic models -> `.proto`

`fastgrpc3 lock`, `check` and `export` take an app (`myapp.main:app`) or a `SchemaRegistry`.

```python
from pydantic import BaseModel
from fastgrpc3 import MethodDef, SchemaRegistry, ServiceDef, field
from fastgrpc3.types import UInt32


class GetOrder(BaseModel):
    order_number: str


class Order(BaseModel):
    order_number: str
    qty: UInt32 = 0  # proto uint32, bounds checked by Pydantic
    note: str | None = field(5, default=None)  # explicit number


registry = SchemaRegistry()
registry.add_service(ServiceDef("Orders", "orders.v1", (MethodDef("Get", GetOrder, Order),)))
```

```bash
fastgrpc3 lock myapp.contract:registry     # number new fields, write proto.lock (commit it)
fastgrpc3 check myapp.contract:registry    # CI: fail if proto.lock is out of date
fastgrpc3 export myapp.contract:registry -o proto/   # one .proto file per package
```

Fields without an explicit number get one the first time they are locked, wherever they are
declared; a removed field's number is reserved for good. Types without a proto equivalent
(`Decimal`, `UUID`, unions…) travel as a JSON string, marked `// json` in the `.proto`.

## Proto first: `.proto` -> Pydantic models

```bash
fastgrpc3 generate proto/ -o src/myapp/gen   # -I third_party/ for extra imports
fastgrpc3 generate proto/ -o src/myapp/gen --check   # CI: fail if not up to date
```

`orders/v1/orders.proto` gives `orders/v1/orders_models.py` (one `ProtoModel` per message,
one open `ProtoEnum` per enum) and `orders/v1/orders_service.py`; `_descriptor.py` embeds the
compiled descriptor, so nothing is compiled at startup. Defaults can live in `pyproject.toml`:

```toml
[tool.fastgrpc3.generate]
proto_paths = ["proto"]
output = "src/myapp/gen"
```

Each service module also holds the contract (`Orders`, to serve with
`GrpcService.implement(Orders)`) and a typed client (`OrdersClient`). Generated code passes
`mypy --strict` and `ruff`. Well-known types become Python types
(`Timestamp` -> `datetime`, `Struct` -> `dict[str, Any]`, wrappers -> `X | None`…).

## Documentation

The guide and the reference live in [`docs/`](docs/index.md) (MkDocs). To build the site
into `site/`, or to serve it locally with live reload:

```bash
uv run --project docs mkdocs build --strict
uv run --project docs mkdocs serve          # http://127.0.0.1:8000
```

## Development

```bash
uv sync                     # install the package and the dev tools
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format --check  # formatting
uv run mypy src tests       # type checking
```

## License

MIT, see [LICENSE](LICENSE).
