"""The bookstore server.

uv run python -m bookstore.app                       # one process, port 50051
uv run fastgrpc3 run bookstore.app:app --workers 2   # two processes sharing the port
"""

import asyncio
import logging
import time
from typing import Any

from fastgrpc3 import CallNext, Context, ErrorInfo, GrpcApp, GrpcError, Insecure, StatusCode

from bookstore.service import service
from bookstore.store import BookNotFound, OutOfStock

logger = logging.getLogger("bookstore")

# Insecure() keeps the example self-contained; use TLS(cert=..., key=...) in production.
app = GrpcApp("bookstore", address="[::]:50051", tls=Insecure())
app.add_service(service)
app.enable_health()  # grpc.health.v1: grpc-health-probe, Kubernetes probes
app.enable_reflection()  # grpc.reflection: grpcurl list / describe / call


@app.middleware
async def log_calls(ctx: Context, call_next: CallNext) -> Any:
    """Log every call with its duration (for streams: until the iterator is returned)."""
    started = time.perf_counter()
    try:
        return await call_next()
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        logger.info("%s from %s in %.2f ms", ctx.method, ctx.peer(), elapsed)


@app.exception_handler(BookNotFound)
def book_not_found(error: Exception) -> GrpcError:
    assert isinstance(error, BookNotFound)
    info = ErrorInfo(
        reason="BOOK_NOT_FOUND", domain="bookstore.example", metadata={"isbn": error.isbn}
    )
    return GrpcError(StatusCode.NOT_FOUND, f"no book {error.isbn}", [info])


@app.exception_handler(OutOfStock)
def out_of_stock(error: Exception) -> GrpcError:
    assert isinstance(error, OutOfStock)
    return GrpcError(StatusCode.FAILED_PRECONDITION, str(error))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(app.run())
