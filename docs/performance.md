# Performance

Measured on 2026-10-29 on an AMD Ryzen 7 PRO 7840U laptop, Python 3.14, protoruf 0.5.0,
against `grpc.aio` with `google.protobuf` (upb) messages, which is what generated stubs do,
and against fastgrpc2 0.5.0. The scripts, the raw data and the exact versions are in
`benches/` (`benches/results/2026-10-29-protoruf-0.5.md`).

## Codec

Encoding and decoding a 20-field message with nesting, lists, an enum and a timestamp
(287 bytes), µs per message:

| | encode | decode |
|-|-------:|-------:|
| fastgrpc3 (decode validates with Pydantic) | 1.05 | 5.06 |
| fastgrpc2 | 8.82 | 16.44 |
| `google.protobuf` + Pydantic (`MessageToDict` + `model_validate`) | 58.34 | 23.47 |

The codec works in Rust, straight from protobuf bytes to Python objects; most of the
decoding time is Pydantic's validation. Fields renamed with `proto_name=`, Pydantic aliases
and `datetime.date` fields are handled in Rust too, so they cost nothing extra; only fields
carried as JSON (types with no protobuf equivalent) and `proto_enum` mappings go through a
Python conversion.

## End to end

Unary echo, one server process pinned to one core, `ghz` as the load generator, requests
per second (concurrency 16):

| | small (9 B) | medium (287 B) | large (23 KB) |
|-|------:|------:|------:|
| `grpc.aio` + upb, no framework | 4,622 | 4,495 | 1,984 |
| fastgrpc2 | 4,230 | 3,402 | 308 |
| fastgrpc3, validation included | 4,565 | 4,398 | 945 |

fastgrpc3 costs a few percent over plain `grpc.aio` on small and medium messages (up to
7 % across runs, within the laptop's frequency noise). On small and medium messages, the
`grpc.aio` layer itself takes most of each call; on large ones, validating 1,000 nested
objects does.

To use more than one core, run several processes: `fastgrpc3 run app:app --workers N`.

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
