"""Message size limits and compression, on both transports."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from fastgrpc3 import Compression, GrpcApp, GrpcClient, GrpcError, GrpcService, Insecure
from fastgrpc3.api.client import Channel
from fastgrpc3.api.errors import StatusCode
from fastgrpc3.testing import TestClient

pytestmark = pytest.mark.usefixtures("transport")

LIMIT = 64 * 1024


class Blob(BaseModel):
    data: bytes = b""
    size: int = 0


class BlobClient(GrpcClient):
    package = "blob.v1"
    service = "Blobs"
    echo = GrpcClient.unary("Echo", Blob, Blob)
    count = GrpcClient.stream_unary("Count", Blob, Blob)


def make_app(compression: Compression | None = None) -> GrpcApp:
    service = GrpcService("Blobs", package="blob.v1")

    @service.unary()
    async def Echo(req: Blob) -> Blob:
        return Blob(data=req.data or b"x" * req.size)

    @service.stream_unary()
    async def Count(requests: AsyncIterator[Blob]) -> Blob:
        return Blob(size=sum([len(r.data) async for r in requests]))

    app = GrpcApp(
        tls=Insecure(), max_recv_bytes=LIMIT, max_send_bytes=LIMIT, compression=compression
    )
    app.add_service(service)
    return app


async def exhausted(call: object) -> None:
    with pytest.raises(GrpcError) as raised:
        await call  # type: ignore[misc]
    assert raised.value.code == StatusCode.RESOURCE_EXHAUSTED


async def test_messages_over_the_limits_are_refused() -> None:
    async with TestClient(make_app()) as tc:
        blobs = tc.client(BlobClient)
        assert len((await blobs.echo(Blob(data=b"x" * 1000))).data) == 1000
        await exhausted(blobs.echo(Blob(data=b"x" * (LIMIT + 1))))  # request
        await exhausted(blobs.echo(Blob(size=LIMIT + 1)))  # response
        await exhausted(blobs.count([Blob(data=b"x"), Blob(data=b"x" * (LIMIT + 1))]))
        assert (await blobs.count([Blob(data=b"x" * 1000)] * 3)).size == 3000


async def test_compression_both_ways_and_the_limit_after_decompression() -> None:
    async with TestClient(make_app(Compression.Gzip)) as tc:
        port = tc.app.bound_port
        async with Channel(f"127.0.0.1:{port}", tls=Insecure(), compression=Compression.Gzip) as gz:
            blobs = BlobClient(gz)
            assert (await blobs.echo(Blob(size=LIMIT // 2))).data == b"x" * (LIMIT // 2)
            # A few hundred bytes on the wire, over the limit once decompressed.
            await exhausted(blobs.echo(Blob(data=b"\0" * (LIMIT * 4))))


async def test_calls_beyond_maximum_concurrent_rpcs_are_refused() -> None:
    release, started = asyncio.Event(), asyncio.Semaphore(0)
    service = GrpcService("Blobs", package="blob.v1")

    @service.unary()
    async def Echo(req: Blob) -> Blob:
        started.release()
        await release.wait()
        return req

    app = GrpcApp(tls=Insecure(), maximum_concurrent_rpcs=2)
    app.add_service(service)
    async with TestClient(app) as tc:
        blobs = tc.client(BlobClient)
        busy = [asyncio.create_task(blobs.echo(Blob(size=i))) for i in range(2)]
        for _ in busy:
            await started.acquire()
        await exhausted(blobs.echo(Blob()))
        release.set()
        assert [b.size for b in await asyncio.gather(*busy)] == [0, 1]
        assert await blobs.echo(Blob(size=5)) == Blob(size=5)  # room again
