# Performance

Measured on 2026-10-29 on an AMD Ryzen 7 PRO 7840U laptop, Python 3.14, protoruf 0.5.1,
against `grpc.aio` with `google.protobuf` (upb) messages, which is what generated stubs do,
and against fastgrpc2 0.5.0. The scripts, the raw data and the exact versions are in
`benches/` (`benches/results/2026-10-29-audit.md`).

## Codec

Encoding and decoding a 20-field message with nesting, lists, an enum and a timestamp
(287 bytes), µs per message:

| | encode | decode |
|-|-------:|-------:|
| fastgrpc3 (decode validates with Pydantic) | 1.05 | 5.01 |
| fastgrpc3, `validate=False` | 1.05 | 2.74 |
| fastgrpc2 | 9.21 | 16.13 |
| `google.protobuf` + Pydantic (`MessageToDict` + `model_validate`) | 57.70 | 24.14 |

The codec works in Rust, straight from protobuf bytes to Python objects; most of the
decoding time is Pydantic's validation (a 1,000-line message decodes in 480 µs validated,
266 µs without validation). Fields renamed with `proto_name=`, Pydantic aliases
and `datetime.date` fields are handled in Rust too, so they cost nothing extra; only fields
carried as JSON (types with no protobuf equivalent) and `proto_enum` mappings go through a
Python conversion.

## End to end

Unary echo, one server process pinned to one core, `ghz` as the load generator, requests
per second (concurrency 16):

| | small (9 B) | medium (287 B) | large (23 KB) |
|-|------:|------:|------:|
| `grpc.aio` + upb, no framework | 4,740 | 4,658 | 1,983 |
| fastgrpc2 | 4,216 | 3,373 | 308 |
| fastgrpc3, validation included | 4,482 | 4,116 | 911 |
| fastgrpc3, `validate_requests=False` | 4,526 | 4,262 | 1,178 |

fastgrpc3 costs a few percent over plain `grpc.aio` on small messages and about 10 % on
medium ones (between 2 % and 12 % across runs: the laptop's frequency varies). On small
and medium messages, the `grpc.aio` layer itself takes most of each call; on large ones,
validating 1,000 nested objects does, which is what `validate_requests=False` saves
(+29 % on large messages, for trusted clients).

To use more than one core, run several processes: `fastgrpc3 run app:app --workers N`.

### On tonic

The same fastgrpc3 server (validation included) on each [transport](server.md#transport),
one core, in one session, lowest of two passes (requests per second):

| | small c=1 | small c=16 | small c=64 | medium c=1 | medium c=16 | medium c=64 |
|-|------:|------:|------:|------:|------:|------:|
| `grpc.aio` | 4,034 | 4,273 | 4,257 | 3,523 | 3,914 | 3,922 |
| tonic | 7,941 | 34,476 | 44,324 | 5,738 | 23,812 | 29,896 |

tonic is about 2× faster for one client at a time, and 6 to 10× under concurrency: the
HTTP/2 and gRPC work is done in Rust, and the asyncio loop takes calls by batches.

## Dependencies

Each `Depends(...)` adds a little to a call. Small message, 16 concurrent calls, one server
core, requests per second (`benches/results/2026-10-29-deps.md`):

| | on the event loop | in a thread (`@blocking`) |
|-|------:|------:|
| no dependency | 4,528 | 4,747 |
| 1 `def` provider | 4,321 (−5 %) | 3,762 (−21 %) |
| 10 `def` providers | 4,218 (−7 %) | 1,977 (−58 %) |
| 10 `def` generators | 3,672 (−19 %) | 1,481 (−69 %) |

`async def` providers cost the same as `def` ones on the loop. Running a provider in a
thread is the price of not blocking the other calls: keep it for providers that do
blocking I/O.

## Startup

`start()` renders and compiles the contract and creates one codec per message, in time
linear in the number of messages: about 180 ms for 400 messages of 10 fields.

## Memory

Streaming 200,000 medium messages each way keeps the server's resident memory flat.

## Run the benchmarks

```bash
uv sync --group bench
go install github.com/bojand/ghz/cmd/ghz@latest

uv run --group bench python benches/codec_bench.py
uv run --group bench python benches/e2e_bench.py
uv run python benches/stream_bench.py
```

See `benches/README.md`. Compare results from the same machine only.
