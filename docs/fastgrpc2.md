# Coming from fastgrpc2

The way services are written is the same: `GrpcApp`, `GrpcService` and its four
decorators, `Context`, `Depends`, `GrpcError`, `Channel`, `GrpcClient.unary(...)`,
`TestClient`, `TLS` / `MTLS` / `Insecure`, `key(n)` and `ProtoField(n)`, `proto_enum`.
What changes is below.

!!! warning "Not wire-compatible"
    fastgrpc3 speaks standard proto3. A fastgrpc3 service and a fastgrpc2 service cannot
    exchange messages: migrate both ends.

## On the wire

| | fastgrpc2 | fastgrpc3 |
|-|-----------|-----------|
| presence | every singular field is `optional` | proto3: `optional` only for `X \| None` |
| `datetime` | `int64` microseconds | `google.protobuf.Timestamp` |
| `date` | `int64` ordinal | `google.type.Date` |
| `dict[str, V]` | JSON string | `map<string, V>` |
| `dict[str, Any]` | JSON string | `google.protobuf.Struct` |
| enum values in the `.proto` | `Status_PAID` | `STATUS_PAID` |
| unknown enum number | error | kept (open enums, with `ProtoEnum`) |
| error details | `type.googleapis.com/fastgrpc2.ErrorInfo` | `type.googleapis.com/google.rpc.ErrorInfo` |
| other types (`Decimal`, unions…) | JSON string | JSON string (unchanged) |

## In the code

- **Field numbers**: fastgrpc2 numbered fields by position, so inserting a field broke the
  wire. fastgrpc3 freezes numbers in `proto.lock` (`fastgrpc3 lock`), or takes them from
  `field(n)` / `key(n)`.
- **Packages**: a contract can span several packages; `fastgrpc3 export` writes one file
  per package (fastgrpc2 wrote one file). `GrpcService(name)` defaults to the package
  `fastgrpc3`.
- **Signatures**: a handler parameter that is neither the request, a `Context` nor a
  `Depends(...)` is an error at import (fastgrpc2 ignored it and failed at the first call).
- **Dependencies**: providers can take sub-dependencies and `Context`, are cached per call,
  and `yield` providers are cleaned up.
- **Middlewares**: `@app.middleware` wraps the handler; `add_interceptor` hooks are still
  there.
- **Errors**: unexpected exceptions are logged (fastgrpc2 logged nothing);
  `@app.exception_handler` maps your exceptions; all ten `google.rpc` detail types are
  built in; `register_detail` indexes by full proto name.
- **Client**: an invalid response raises `GrpcError(INTERNAL)` instead of returning `None`;
  calls are typed.
- **Health and reflection**: `Watch` is implemented, an unknown service answers
  `NOT_FOUND`, and reflection needs no `grpcio-reflection` package.
- **Proto first**: `fastgrpc3 generate` creates the models, contracts and clients from
  `.proto` files.
