# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (0.x: the API may change between minor
versions).

## [0.1.0] - 2026-10-30

First release: a FastAPI-like gRPC framework for Python on the protoruf codec.

### Added

- Contracts both ways: Pydantic models to `.proto` (code first, field numbers pinned in
  `proto.lock`), and `.proto` files to generated models, services and typed clients
  (`fastgrpc3 generate`); `fastgrpc3 export`, `lock`, `check`, `breaking --against REF`.
- Server: the four kinds of call from `async` handlers, dependency injection
  (`Depends`, sub-dependencies, per-call cache, `yield` cleanup, overrides, `@blocking`),
  hooks, middlewares, exception handlers, `GrpcError` with the standard `google.rpc`
  details, deadline propagation, request validation (`validate_requests=False` to skip it).
- Typed clients (`GrpcClient`, `Channel`): TLS, mTLS, bearer tokens, retries, deadlines.
- Explicit transport security (`TLS`, `MTLS`, `Insecure`), size limits, compression,
  keepalive, admission limit.
- Health checking (`grpc.health.v1`), server reflection (v1, v1alpha), interactive docs
  (Swagger UI, OpenAPI, JSON gateway: extra `fastgrpc3[docs]`).
- `fastgrpc3 run app:app [--workers N] [--transport grpc_aio|tonic]`, `TestClient`.
- Two transports: `grpc.aio` (default) and tonic, a Rust server (extra `fastgrpc3[tonic]`,
  experimental): `GrpcApp(transport=...)`, `FASTGRPC_TRANSPORT`, `available_transports()`.

[0.1.0]: https://github.com/EdwinAlkins/fastgrpc3/releases/tag/v0.1.0
