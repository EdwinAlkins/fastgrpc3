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
