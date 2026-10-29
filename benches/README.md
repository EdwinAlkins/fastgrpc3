# Benchmarks

Comparison points (fastgrpc2, `google.protobuf`) are in the `bench` dependency group, and
the end-to-end load comes from [`ghz`](https://ghz.sh):

```bash
uv sync --group bench
go install github.com/bojand/ghz/cmd/ghz@latest      # end-to-end only

uv run --group bench python benches/codec_bench.py   # P1, P2: µs per message
uv run --group bench python benches/e2e_bench.py     # P3, P4: requests/s, latency
uv run python benches/stream_bench.py                # P5: memory on long streams
```

Each script takes `--out FILE.json` to save its results with the environment. Results and
their reading live in `results/<date>.md`; compare runs from the same machine only.

| File | What |
|------|------|
| `contract.py` | the small, medium (20 fields, nested) and large (1,000 lines) messages |
| `codec_bench.py` | fastgrpc3 codec vs fastgrpc2 vs upb (raw, and with Pydantic) |
| `e2e_servers.py` | echo servers: `raw` (`grpc.aio` + upb), `fastgrpc2`, `fastgrpc3` |
| `e2e_bench.py` | runs each server pinned to one CPU and `ghz` on the others |
| `stream_bench.py` | 200,000 messages each way, server RSS sampled along the stream |
