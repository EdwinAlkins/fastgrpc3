# fastgrpc3

A FastAPI-like gRPC framework for Python: async handlers, Pydantic models, dependency
injection and typed clients, on top of `grpc.aio`. Messages are encoded and decoded in Rust
by [protoruf](https://github.com/EdwinAlkins/protoruf), straight between protobuf bytes and
Pydantic models.

!!! warning "Early development"
    fastgrpc3 is at version 0.1: the API may still change.

## Two ways to write a contract

| | Code first | Proto first |
|-|------------|-------------|
| Source of truth | Pydantic models in Python | `.proto` files |
| You write | models and handlers | `.proto` files and handlers |
| fastgrpc3 gives | the `.proto` files (`fastgrpc3 export`) | Pydantic models, a service contract and a typed client (`fastgrpc3 generate`) |
| Field numbers | `field(n)`, or frozen in `proto.lock` | those of the `.proto` |

Both produce the same kind of models, and a server can mix them. Either way the wire is
standard protobuf, so clients in any language can call the service.

## A service in a few lines

```python
from collections.abc import AsyncIterator

from fastgrpc3 import GrpcApp, GrpcService, Insecure, ProtoModel, field


class Greeting(ProtoModel):
    name: str = field(1)


class Reply(ProtoModel):
    text: str = field(1)


service = GrpcService("Greeter", package="hello.v1")


@service.unary()
async def SayHello(req: Greeting) -> Reply:
    return Reply(text=f"Hello, {req.name}!")


@service.unary_stream()
async def SayHelloTwice(req: Greeting) -> AsyncIterator[Reply]:
    for _ in range(2):
        yield Reply(text=f"Hello, {req.name}!")


app = GrpcApp(address="[::]:50051", tls=Insecure())
app.add_service(service)
```

```bash
fastgrpc3 run hello:app
```

[Getting started](getting-started.md) builds a complete service and its client step by
step; `examples/` in the repository holds a bookstore service that uses every feature,
with its client and tests.

## Install

```bash
pip install fastgrpc3                 # or: uv add fastgrpc3
pip install "fastgrpc3[tonic]"        # with the Rust transport (see Server > Transport)
pip install "fastgrpc3[docs]"         # with Swagger UI and the JSON gateway
```

Python 3.12 or later (tested on 3.12, 3.13 and 3.14). The dependencies are `grpcio`,
`protoruf` and `pydantic`; there is no `protoc` to install.

## What is in the box

- The four kinds of gRPC call, each from a decorated `async` function.
- Dependency injection with `Depends(...)`: sub-dependencies, per-call cache, cleanup after
  the response for `yield` dependencies, overrides for tests.
- Pre-call hooks, middlewares around handlers, exception handlers.
- Rich errors with the standard `google.rpc` details (`ErrorInfo`, `BadRequest`…).
- Deadline propagation from a call to the calls its handler makes.
- Health checking (`grpc.health.v1`) and server reflection (`grpcurl`), built in.
- Interactive docs: Swagger UI generated from the models, with a JSON gateway to try
  the RPCs (`fastgrpc3[docs]`).
- `TLS`, `MTLS` or `Insecure`, always explicit; no credentials over an insecure channel.
- `fastgrpc3 run --workers N` to serve on several processes.
- Two transports: `grpc.aio`, or tonic, a Rust server several times faster per core
  (`--transport tonic`, `fastgrpc3[tonic]`, experimental); see
  [Transport](server.md#transport).
- `TestClient` to test a service against a real local server.

## Develop fastgrpc3

```bash
uv sync                     # install the package and the dev tools
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format --check  # formatting
uv run mypy src tests       # type checking
uv run --project docs mkdocs build --strict   # this documentation
```
