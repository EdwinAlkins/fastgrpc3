# fastgrpc3

A FastAPI-like gRPC framework for Python: async handlers, Pydantic models, dependency
injection and typed clients, on top of `grpc.aio`.

> **Status: early development.** Contracts, server and client work; health checking,
> server reflection and `run --workers` are still to come. The API may change.

- **Two ways to declare a contract.** Start from `.proto` files and generate the Pydantic
  models, services and clients, or write Pydantic models and get the `.proto` generated.
- **Fast protobuf codec.** Encoding and decoding run in Rust through
  [protoruf](https://github.com/EdwinAlkins/protoruf), straight between protobuf bytes and
  Python objects, without any JSON step.
- **Standard gRPC.** Real protobuf on the wire, standard error details, health checking and
  server reflection, so clients in any language can talk to it.

Requires Python 3.12 or later (tested on 3.12, 3.13 and 3.14).

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
