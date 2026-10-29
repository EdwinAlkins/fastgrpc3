"""`fastgrpc3 run app:app --workers N`: N server processes sharing one port.

Each worker imports the app itself (spawned, not forked: `grpc` does not survive a fork) and
binds the same address with `SO_REUSEPORT`; the kernel spreads connections over them (Linux).
SIGINT / SIGTERM to the parent stop every worker gracefully; if a worker dies, the others are
stopped too and the exit status is 1.
"""

import asyncio
import multiprocessing
import multiprocessing.connection
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from fastgrpc3.api.app import GrpcApp

__all__ = ["serve"]

REUSE_PORT = ("grpc.so_reuseport", 1)


def serve(
    load: Callable[[str], GrpcApp], target: str, address: str | None, workers: int, grace: float
) -> int:
    """Run the app of `target` in this process, or in `workers` processes; return the status."""
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    app = load(target)
    if address is not None:
        app.address = address
    if workers == 1:
        asyncio.run(app.run(grace))
        return 0
    if app.address.rpartition(":")[2] == "0":
        raise ValueError("several workers need a fixed port, not :0")
    if app.docs is not None:
        raise ValueError("enable_docs serves one HTTP port: run the docs with a single worker")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_worker,
            args=(target, app.address, grace, str(Path.cwd())),
            name=f"fastgrpc3-worker-{index}",
        )
        for index in range(workers)
    ]
    return _supervise(processes)


def _supervise(processes: list[Any]) -> int:
    stopping = False

    def stop(signum: int, frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    # Installed before the workers start, so that no signal is missed.
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    status = 0
    try:
        for process in processes:
            process.start()
        alive = list(processes)
        while alive:
            multiprocessing.connection.wait([p.sentinel for p in alive], timeout=0.2)
            exited = [p for p in alive if not p.is_alive()]
            if exited and not stopping:
                status = 1 if any(p.exitcode != 0 for p in exited) else 0
                stopping = True
            if stopping:
                for process in alive:
                    if process.is_alive():
                        process.terminate()  # SIGTERM: the worker stops gracefully
            alive = [p for p in alive if p.is_alive()]
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return status


def _worker(target: str, address: str, grace: float, cwd: str) -> None:
    sys.path.insert(0, cwd)
    from fastgrpc3.cli import load_app  # in the worker: import the app afresh

    app = load_app(target)
    app.address = address
    app.options.append(REUSE_PORT)
    asyncio.run(app.run(grace))
