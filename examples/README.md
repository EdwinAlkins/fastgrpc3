# fastgrpc3 examples

`bookstore/` is a complete gRPC service written with fastgrpc3, code first: Pydantic models
define the contract, async functions implement it, and a typed client calls it. It covers
the four kinds of gRPC call, dependency injection, errors with standard details, health
checking and server reflection.

```text
examples/
├── pyproject.toml        uses the fastgrpc3 of this repository (editable)
├── bookstore/
│   ├── models.py         the messages: Book, GetBookRequest, StockChange…
│   ├── store.py          an in-memory store standing for a database
│   ├── service.py        the Bookstore service: one handler per RPC
│   ├── app.py            the server: GrpcApp, middleware, exception handlers, health, reflection
│   └── client.py         BookstoreClient and a demo calling every RPC
└── tests/
    └── test_bookstore.py the service tested through TestClient
```

## Run it

From this directory:

```bash
uv sync                                   # Python 3.12+, installs fastgrpc3 from ..
uv run python -m bookstore.app            # the server, on port 50051
```

In another terminal:

```bash
uv run python -m bookstore.client
```

```text
GetBook: L'Étranger by Albert Camus, 7.50 €
GetBook unknown: NOT_FOUND 'no book 0000000000', reason BOOK_NOT_FOUND
ListBooks(ESSAY): ['Le Mythe de Sisyphe']
CreateBook as anonymous: PERMISSION_DENIED
CreateBook as admin: La Peste
ImportBooks: 1 imported, skipped ['9782070368228']
AdjustStock: 9782070360024 -> 7
AdjustStock: 9782070360024 -> 5
AdjustStock stopped: FAILED_PRECONDITION '9782070321803: only 1 left'
```

The tests start the server on a free local port themselves:

```bash
uv run pytest
```

## The RPCs

| RPC | Kind | Request → response | What it shows |
|-----|------|--------------------|---------------|
| `GetBook` | unary | `GetBookRequest` → `Book` | a dependency (`Depends(get_store)`); a business exception (`BookNotFound`) mapped to `NOT_FOUND` with an `ErrorInfo` detail |
| `ListBooks` | server stream | `ListBooksRequest` → stream of `Book` | an async generator handler; an `optional` enum filter |
| `CreateBook` | unary | `CreateBookRequest` → `Book` | sub-dependencies (`require_admin` → `current_user` → `Context`); `PERMISSION_DENIED`, `ALREADY_EXISTS`; request validation (`min_length`) |
| `ImportBooks` | client stream | stream of `Book` → `ImportSummary` | reading an `AsyncIterator` of requests |
| `AdjustStock` | bidirectional | stream of `StockChange` → stream of `StockLevel` | one response per request; an error in the middle of a stream |

## How the pieces fit

**The contract** (`models.py`). Each message is a `ProtoModel` with a field number on every
field (`field(1)`); Pydantic constraints still apply (`field(1, min_length=10)`). The
`UInt32` / `Int32` markers pick the proto integer type and check its bounds. `Genre` is a
`ProtoEnum`: its values become `GENRE_NOVEL`… in the `.proto`, and an unknown number sent
by a newer peer is kept instead of failing.

**The service** (`service.py`). `GrpcService("Bookstore", package="bookstore.v1")` and one
decorator per call kind: `@service.unary()`, `unary_stream()`, `stream_unary()`,
`stream_stream()`. The RPC name is the function's. fastgrpc3 reads the signature when the
module is imported: the parameter annotated with a model is the request (an
`AsyncIterator[Model]` for a client stream), `Context` gives the call's metadata and
deadline, and `Depends(...)` parameters are filled by the dependency injection.

**The server** (`app.py`). `GrpcApp(tls=Insecure())` serves the service; the transport is
always explicit (`TLS(cert=..., key=...)` in production). `@app.middleware` wraps every
call (here, a log line with its duration), `@app.exception_handler(BookNotFound)` turns a
business exception into a gRPC status, and `enable_health()` / `enable_reflection()` add the
standard health and reflection services. When it starts, the app renders and compiles the
`.proto` of its services; the models are encoded and decoded by protoruf's Rust codec.

**The client** (`client.py`). `BookstoreClient` declares the RPCs with
`GrpcClient.unary(...)` etc. and the same models as the server. A unary call is awaited, a
server stream is iterated, a client stream takes an iterable or an async iterator. A failed
call raises `GrpcError`, whose `detail(ErrorInfo)` gives the standard error detail.

**The tests** (`tests/test_bookstore.py`). `TestClient(app)` starts the real server on
`127.0.0.1` and a free port; `app.dependency_overrides[get_store]` gives each test a
fresh store.

## Interactive docs

`python -m bookstore.app` also serves Swagger UI on <http://127.0.0.1:8080/docs>
(`app.enable_docs(gateway=True, metadata=["x-user"])` in `app.py`): every RPC with its
request and response models, constraints and descriptions, generated from the code.
"Try it out" sends JSON through a gateway that calls the real gRPC service, so hooks,
middlewares and errors apply:

- `GetBook` with `{"isbn": "9782070360024"}`, then with an unknown ISBN: `404`, and the
  `ErrorInfo` detail in the `Status` body;
- `CreateBook` without the `x-user` header: `403 PERMISSION_DENIED`; with `x-user: admin`:
  the new book;
- `ListBooks` with `{"genre": 2}`: the essays, one JSON object per line (NDJSON);
- `ImportBooks` and `AdjustStock` take NDJSON too, one request per line.

The same calls without a browser:

```bash
curl -X POST localhost:8080/bookstore.v1.Bookstore/GetBook -d '{"isbn": "9782070360024"}'
curl -X POST localhost:8080/bookstore.v1.Bookstore/CreateBook -H 'x-user: admin' \
     -d '{"book": {"isbn": "9782070368228", "title": "La Peste"}}'
uv run fastgrpc3 openapi bookstore.app:app -o openapi.json   # the OpenAPI document
```

The gateway is a proxy to every RPC: it is off by default, listens on 127.0.0.1, and is
meant for development.

## Other ways to look at it

```bash
# The contract as .proto files: bookstore/v1/bookstore.proto, plus health and reflection.
uv run fastgrpc3 export bookstore.app:app -o proto/

# Instead of `python -m bookstore.app`: two server processes sharing port 50051 (Linux).
uv run fastgrpc3 run bookstore.app:app --workers 2
```

With a server running, reflection and health answer the usual tools
([grpcurl](https://github.com/fullstorydev/grpcurl),
[grpc-health-probe](https://github.com/grpc-ecosystem/grpc-health-probe)):

```bash
grpcurl -plaintext localhost:50051 list
grpcurl -plaintext -d '{"isbn": "9782070360024"}' localhost:50051 bookstore.v1.Bookstore/GetBook
grpc-health-probe -addr=localhost:50051 -service=bookstore.v1.Bookstore
```

`CreateBook` trusts an `x-user` header to keep the example short. A real service would
authenticate the caller (TLS client certificates, or a token checked by an
`app.add_interceptor` hook); fastgrpc3 refuses to send credentials such as `authorization`
over an insecure channel.

See [the documentation](../docs/index.md) for everything else: the proto-first mode
(`fastgrpc3 generate`), `proto.lock`, the type mapping, TLS and the client options.
