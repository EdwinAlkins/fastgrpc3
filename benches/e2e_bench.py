"""End-to-end benchmark (criteria P3, P4): unary echo, requests per second and latency.

    uv run --group bench python benches/e2e_bench.py [--duration 5] [--out results.json]

Each server runs alone in its own process, pinned to one CPU; the load comes from `ghz` (a
Go client, so that the Python client is not what is measured), pinned to other CPUs.
Requests are pre-encoded by each library (`ghz --binary`), so every server receives its own
wire format.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from contract import SIZES
from environment import environment, markdown_table

HERE = Path(__file__).parent
MODES = ("raw", "fastgrpc2", "fastgrpc3")
SERVER_CPU = "0"
CLIENT_CPUS = "2-7"


def ghz() -> str:
    path = shutil.which("ghz") or shutil.which("ghz", path=str(Path.home() / "go" / "bin"))
    if path is None:
        raise SystemExit("ghz is not installed: go install github.com/bojand/ghz/cmd/ghz@latest")
    return path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def pinned(cpus: str, command: list[str]) -> list[str]:
    return ["taskset", "-c", cpus, *command] if shutil.which("taskset") else command


def wait_for_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"server on port {port} did not start")


def run_ghz(data: Path, port: int, size: str, concurrency: int, duration: float) -> dict[str, Any]:
    command = pinned(
        CLIENT_CPUS,
        [
            ghz(),
            "--insecure",
            "--protoset", str(data / "bench.protoset"),
            "--call", f"bench.v1.Bench/{size.capitalize()}",
            "--binary-file", str(data / f"{size}.bin"),
            "--concurrency", str(concurrency),
            "--connections", str(min(concurrency, 4)),
            "--duration", f"{duration}s",
            "--format", "json",
            f"127.0.0.1:{port}",
        ],
    )  # fmt: skip
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    report = json.loads(output)
    errors = sum(count for code, count in report["statusCodeDistribution"].items() if code != "OK")
    # With --duration, the calls in flight when time is up are cut: at most `concurrency`.
    if errors > concurrency:
        raise RuntimeError(f"{errors} failed calls: {report['statusCodeDistribution']}")
    latencies = {d["percentage"]: d["latency"] / 1e6 for d in report["latencyDistribution"]}
    return {"rps": report["rps"], "p50 ms": latencies[50], "p99 ms": latencies[99]}


def bench_mode(
    mode: str, data: Path, concurrencies: list[int], duration: float
) -> dict[str, dict[str, Any]]:
    port = free_port()
    command = pinned(SERVER_CPU, [sys.executable, str(HERE / "e2e_servers.py"), mode, str(port)])
    server = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        wait_for_port(port)
        results = {}
        for size in SIZES:
            run_ghz(data, port, size, 4, 1.0)  # warm up
            for concurrency in concurrencies:
                results[f"{size} c={concurrency}"] = run_ghz(
                    data, port, size, concurrency, duration
                )
        return results
    finally:
        server.terminate()
        server.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per measure")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 16, 64])
    parser.add_argument("--modes", nargs="+", default=list(MODES), choices=MODES)
    parser.add_argument("--out", type=Path, help="write the results as JSON")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as directory:
        results: dict[str, dict[str, dict[str, Any]]] = {}
        for mode in args.modes:
            data = Path(directory) / ("fastgrpc2" if mode == "fastgrpc2" else "fastgrpc3")
            if not data.exists():
                prepare = [sys.executable, str(HERE / "e2e_servers.py"), mode, "--prepare"]
                subprocess.run([*prepare, str(data)], check=True)
            print(f"benchmarking {mode}...", file=sys.stderr)
            results[mode] = bench_mode(mode, data, args.concurrency, args.duration)

    for measure in ("rps", "p50 ms", "p99 ms"):
        rows = {
            mode: {k: v[measure] for k, v in by_point.items()} for mode, by_point in results.items()
        }
        columns = list(next(iter(rows.values())))
        transposed = {column: {mode: rows[mode][column] for mode in rows} for column in columns}
        print(markdown_table(measure, list(rows), transposed))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "environment": environment(),
            "settings": {
                "duration": args.duration,
                "server_cpu": SERVER_CPU,
                "client_cpus": CLIENT_CPUS,
                "pid": os.getpid(),
            },
            "results": results,
        }
        args.out.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
