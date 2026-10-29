"""Long streams (criterion P5): server memory stays flat while it streams many messages.

    uv run python benches/stream_bench.py [--messages 200000] [--out results.json]

A fastgrpc3 server streams `--messages` medium messages to a fastgrpc3 client, in both
directions (server stream, then client stream); the server's resident memory is sampled as
the stream goes.
"""

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from contract import make_models, sample
from environment import environment

HERE = Path(__file__).parent
SAMPLES = 10


def app(port: int) -> Any:
    from fastgrpc3 import GrpcApp, GrpcService, Insecure

    models = make_models()
    Order, Small = models.medium, models.small
    service = GrpcService("Streams", package="bench.v1")
    template = sample(models, "medium")

    @service.unary_stream()
    async def Down(request: Small) -> AsyncIterator[Order]:  # type: ignore[valid-type]
        for _ in range(request.id):  # type: ignore[attr-defined]
            yield template

    @service.stream_unary()
    async def Up(requests: AsyncIterator[Order]) -> Small:  # type: ignore[valid-type]
        count = 0
        async for _ in requests:
            count += 1
        return Small(id=count)

    application = GrpcApp(tls=Insecure(), address=f"127.0.0.1:{port}")
    application.add_service(service)
    return application, models, template


def rss_mib(pid: int) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("no VmRSS")


async def measure(port: int, pid: int, messages: int) -> dict[str, list[float]]:
    from fastgrpc3 import Channel, GrpcClient, Insecure

    _, models, template = app(0)

    class Streams(GrpcClient):
        package = "bench.v1"
        service = "Streams"
        down = GrpcClient.unary_stream("Down", models.small, models.medium)
        up = GrpcClient.stream_unary("Up", models.medium, models.small)

    every = messages // SAMPLES
    curves: dict[str, list[float]] = {"server stream": [], "client stream": []}
    async with Channel(f"127.0.0.1:{port}", tls=Insecure(), default_timeout=None) as channel:
        client = Streams(channel)
        count = 0
        async for _ in client.down(models.small(id=messages)):
            if count % every == 0:
                curves["server stream"].append(rss_mib(pid))
            count += 1
        assert count == messages

        async def requests() -> AsyncIterator[Any]:
            for index in range(messages):
                if index % every == 0:
                    curves["client stream"].append(rss_mib(pid))
                yield template

        assert (await client.up(requests())).id == messages
    return curves


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--messages", type=int, default=200_000)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve is not None:
        application, _, _ = app(args.serve)
        asyncio.run(application.run())
        return

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen([sys.executable, __file__, "--serve", str(port)])
    try:
        for _ in range(200):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        started = time.perf_counter()
        curves = asyncio.run(measure(port, server.pid, args.messages))
        elapsed = time.perf_counter() - started
    finally:
        server.terminate()
        server.wait(timeout=10)

    print(f"{args.messages:,} medium messages each way in {elapsed:.1f} s")
    for name, values in curves.items():
        growth = values[-1] - values[1]  # from the first tenth on, after warm-up
        shown = " ".join(f"{value:.1f}" for value in values)
        print(f"{name}: server RSS MiB at each tenth: {shown} (growth {growth:+.1f})")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"environment": environment(), "messages": args.messages, "rss_mib": curves}
        args.out.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
