# fastgrpc3

A FastAPI-like gRPC framework for Python: async handlers, Pydantic models, dependency
injection and typed clients, on top of `grpc.aio`.

> **Status: early development.** Nothing is usable yet; the API below is the target.

- **Two ways to declare a contract.** Start from `.proto` files and generate the Pydantic
  models, services and clients, or write Pydantic models and get the `.proto` generated.
- **Fast protobuf codec.** Encoding and decoding run in Rust through
  [protoruf](https://github.com/EdwinAlkins/protoruf), straight between protobuf bytes and
  Python objects, without any JSON step.
- **Standard gRPC.** Real protobuf on the wire, standard error details, health checking and
  server reflection, so clients in any language can talk to it.

Requires Python 3.12 or later (tested on 3.12, 3.13 and 3.14).

## Code first: Pydantic models -> `.proto`

The contract layer exists; the server and the client do not yet.

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
