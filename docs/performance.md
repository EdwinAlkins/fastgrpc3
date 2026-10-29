# Performance

Measured on 2026-10-29 on an AMD Ryzen 7 PRO 7840U laptop, Python 3.14, against
`grpc.aio` with `google.protobuf` (upb) messages, which is what generated stubs do, and
against fastgrpc2 0.5.0. The scripts, the raw data and the exact versions are in
`benches/` (`benches/results/2026-10-29.md`).

## Codec

Encoding and decoding a 20-field message with nesting, lists, an enum and a timestamp
(287 bytes), µs per message:

| | encode | decode |
|-|-------:|-------:|
| fastgrpc3 (decode validates with Pydantic) | 1.06 | 4.98 |
| fastgrpc2 | 9.16 | 16.23 |
| `google.protobuf` + Pydantic (`MessageToDict` + `model_validate`) | 58.36 | 24.50 |

The codec works in Rust, straight from protobuf bytes to Python objects; most of the
decoding time is Pydantic's validation.

## End to end

Unary echo, one server process pinned to one core, `ghz` as the load generator, requests
per second (concurrency 16):

| | small (9 B) | medium (287 B) | large (23 KB) |
|-|------:|------:|------:|
| `grpc.aio` + upb, no framework | 4,729 | 4,643 | 1,982 |
| fastgrpc2 | 4,349 | 3,481 | 312 |
| fastgrpc3, validation included | 4,723 | 4,336 | 930 |

fastgrpc3 costs 0.1–1.3 % over plain `grpc.aio` on small messages and 6–7 % on medium
ones. On small and medium messages, the `grpc.aio` layer itself takes most of each call;
on large ones, validating 1,000 nested objects does.

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
