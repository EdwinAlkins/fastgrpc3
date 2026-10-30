# Performance

fastgrpc3 is built to be fast at every layer: protobuf is encoded and decoded in Rust
([protoruf](https://github.com/EdwinAlkins/protoruf)), the framework adds almost nothing
over plain `grpc.aio`, and with the tonic transport the whole HTTP/2 and gRPC layer runs
in Rust too.

## At a glance

On **one CPU core**, serving a unary echo to 64 concurrent clients:

| | calls per second | CPU per call | p99 latency |
|---|--:|--:|--:|
| **fastgrpc3 on tonic** | **62,240** | **13 µs** | **2.0 ms** |
| fastgrpc3 on `grpc.aio` | 4,430 | 226 µs | 15.9 ms |
| `grpc.aio` + generated stubs, no framework | 4,571 | 219 µs | 18.0 ms |
| fastgrpc2 | 4,108 | 244 µs | 19.4 ms |

- **14× more calls per core** than the same app on `grpc.aio`, **15× more** than
  fastgrpc2, and 13.6× more than hand-written `grpc.aio` stubs without any framework.
- **17× less CPU per call** (13 µs instead of 226 µs): fewer servers for the same traffic.
- **8× lower tail latency** (p99 16 ms → 2 ms).
- **Same code**: switching transport is `fastgrpc3 run app:app --transport tonic`; handlers,
  dependencies, middlewares and errors do not change.

The tonic figures are capped by the load generator (`ghz` peaks around 62,000 calls/s on
the CPUs it was given): the server still had headroom, which the CPU per call shows.

## One core, every size

Medians of three runs in alternating order, one server process pinned to one core, `ghz`
on other cores, 64 concurrent clients (calls per second):

| | small (9 B) | medium (287 B) | large (23 KB) |
|---|--:|--:|--:|
| `grpc.aio` + upb, no framework | 4,571 | 4,474 | 1,922 |
| fastgrpc2 | 4,108 | 3,347 | 312 |
| fastgrpc3 on `grpc.aio` | 4,430 | 4,132 | 954 |
| fastgrpc3 on `grpc.aio`, `validate_requests=False` | 4,436 | 4,310 | 1,211 |
| **fastgrpc3 on tonic** | **62,240** | **40,943** | 1,176 |
| **fastgrpc3 on tonic**, `validate_requests=False` | **62,165** | **44,846** | **1,630** |

- **Small and medium messages**: 10 to 14× `grpc.aio`. The transport was the bottleneck,
  and tonic removes it.
- **Large messages** (a 23 KB message of about 1,000 nested objects): the time goes into
  building and validating the Python objects, so the transport matters less (1.2×; 1.35×
  without validation). Versus fastgrpc2, fastgrpc3 is 3 to 5× faster here thanks to its Rust
  codec.
- **On `grpc.aio`**, fastgrpc3 costs about 3 % over plain stubs on small messages, while
  giving Pydantic models, validation, dependency injection and typed clients.

With a single client at a time (concurrency 1), tonic answers in 0.06 ms instead of 0.18 ms
(p50), about 2× more calls.

## Latency

p50 / p99 in milliseconds, one core:

| | small, 16 clients | small, 64 clients | medium, 64 clients | small, 256 clients |
|---|--:|--:|--:|--:|
| fastgrpc3 on `grpc.aio` | 3.41 / 4.07 | 14.23 / 15.89 | 15.37 / 18.65 | 58.20 / 71.41 |
| **fastgrpc3 on tonic** | **0.20 / 0.52** | **0.51 / 2.00** | **1.16 / 3.04** | **1.83 / 7.36** |

## Several cores

`fastgrpc3 run app:app --workers N` starts one process per core, sharing the port. Calls
per second and CPU per call, small messages, 64 clients:

| | 2 workers | 4 workers |
|---|--:|--:|
| fastgrpc3 on `grpc.aio` | 8,555 — 233 µs | 18,590 — 170 µs |
| **fastgrpc3 on tonic** | **91,972 — 17 µs** | 73,847 — 20 µs |

With tonic, a single core already outruns the load generator, so the multi-core numbers
measure the client more than the server (with 4 workers `ghz` had fewer CPUs left). The CPU
per call shows the real cost: about 10× lower than `grpc.aio` at any number of workers.

## A real application

Dependencies and middlewares cost little, on both transports (medium message, 64 clients,
one core):

| | calls per second on tonic | CPU per call on tonic | calls per second on `grpc.aio` |
|---|--:|--:|--:|
| plain handler | 41,428 | 22.0 µs | 4,044 |
| 1 dependency | 39,338 | 23.6 µs | 3,909 |
| 3 dependencies | 38,831 | 24.1 µs | 3,911 |
| 10 dependencies | 35,812 | 26.4 µs | 3,865 |
| 1 middleware | 38,933 | 24.0 µs | 3,973 |
| 3 middlewares | 38,153 | 24.4 µs | 3,939 |

Each dependency adds about 0.4 µs per call; the first middleware 1.8 µs (the call leaves
the fast path that skips building a `Context`), the next ones about 0.2 µs.

Providers declared `@blocking` run in a thread, so that blocking I/O does not stall the
other calls; that has a cost of its own (`benches/results/2026-10-29-deps.md`): keep it for
providers that really block.

## Codec

Encoding and decoding a 20-field message with nesting, lists, an enum and a timestamp
(287 bytes), µs per message (protoruf 0.5.1; 0.5.3 is a little faster still):

| | encode | decode |
|-|-------:|-------:|
| **fastgrpc3** (decode validates with Pydantic) | **1.05** | **5.01** |
| **fastgrpc3**, `validate=False` | **1.05** | **2.74** |
| fastgrpc2 | 9.21 | 16.13 |
| `google.protobuf` + Pydantic (`MessageToDict` + `model_validate`) | 57.70 | 24.14 |

Encoding is 9× faster than fastgrpc2 and 55× faster than going through `google.protobuf`
and Pydantic. The codec works in Rust, straight from protobuf bytes to Python objects; most
of the decoding time is Pydantic's validation, which `validate=False` skips for trusted
data. Renamed fields, aliases and dates are handled in Rust too, at no extra cost.

## Memory and lifecycle

- **Streams**: 200,000 medium messages each way keep the server's memory flat, on both
  transports, with a fast or a slow client (backpressure holds the producer).
- **Footprint**: a fastgrpc3 server on tonic uses about 10 MiB less resident memory than on
  `grpc.aio` (53–68 MiB against 63–78 MiB in these runs).
- **Startup**: `start()` compiles the contract and creates one codec per message, in time
  linear in the number of messages: about 180 ms for 400 messages of 10 fields; a small
  app starts in about 2 ms.
- **Stop**: graceful, bounded by its grace period on both transports.

## How these numbers were measured

AMD Ryzen 7 PRO 7840U laptop on mains, idle, Python 3.14, 30/10/2026; fastgrpc3 0.1.0,
fastgrpc3-transport 0.1.0 (built with PGO, as the published Linux wheels), protoruf 0.5.3,
grpcio 1.85.0, pydantic 2.14.1, fastgrpc2 0.5.0. Each server runs alone in its process,
pinned to CPU 0 (CPUs 0, 2, 4, 6 for the workers); `ghz` sends pre-encoded requests from
other CPUs over 4 connections, 4 s per point after a warm-up; three repetitions in
alternating order, medians reported, spread mostly under 5 %. A run with failed calls is
rejected. CPU per call counts every thread and process of the server.

The scripts and the raw data are in the repositories: `benches/` here (codec, end to end,
dependencies), and `bench/campaign.py` with `bench/results/2026-10-30-lot9.md` in
[fastgrpc3-transport](https://github.com/EdwinAlkins/fastgrpc3-transport). Compare results
from the same machine only.

```bash
uv sync --group bench --extra tonic
go install github.com/bojand/ghz/cmd/ghz@latest

uv run --group bench python benches/codec_bench.py
uv run --group bench python benches/e2e_bench.py           # FASTGRPC_TRANSPORT=tonic for tonic
uv run python benches/stream_bench.py
```
