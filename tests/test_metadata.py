"""Response headers and trailers, and the peer, as a raw `grpc` client sees them."""

from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest
from pydantic import BaseModel

from fastgrpc3 import Context, GrpcApp, GrpcError, GrpcService, Insecure, StatusCode
from fastgrpc3.testing import TestClient

pytestmark = pytest.mark.usefixtures("transport")


class Empty(BaseModel):
    pass


def make_app() -> GrpcApp:
    service = GrpcService("Meta", package="meta.v1")

    def trailers(ctx: Context) -> None:
        ctx.set_trailing_metadata(
            [("x-trailer", "t1"), ("x-trailer", "t2"), ("x-blob-bin", b"\x00\xff")]
        )

    @service.unary()
    async def One(req: Empty, ctx: Context) -> Empty:
        await ctx.send_initial_metadata({"x-header": "h", "x-peer": ctx.peer()})
        trailers(ctx)
        return Empty()

    @service.unary_stream()
    async def Many(req: Empty, ctx: Context) -> AsyncIterator[Empty]:
        await ctx.send_initial_metadata({"x-header": "h"})
        yield Empty()
        yield Empty()
        trailers(ctx)

    @service.unary()
    async def Refuse(req: Empty, ctx: Context) -> Empty:
        trailers(ctx)
        raise GrpcError(StatusCode.NOT_FOUND, "nope")

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    return app


def unary(tc: TestClient, method: str) -> "grpc.aio.UnaryUnaryMultiCallable[bytes, bytes]":
    return tc.channel.grpc_channel.unary_unary(f"/meta.v1.Meta/{method}")


def values(metadata: grpc.aio.Metadata | None, key: str) -> list[str | bytes]:
    return [value for name, value in metadata or () if name == key]


def check_trailers(trailing: grpc.aio.Metadata | None) -> None:
    assert values(trailing, "x-trailer") == ["t1", "t2"]
    assert values(trailing, "x-blob-bin") == [b"\x00\xff"]


async def test_a_unary_call_sends_headers_trailers_and_sees_its_peer() -> None:
    async with TestClient(make_app()) as tc:
        call = unary(tc, "One")(b"")
        assert await call == b""
        initial = await call.initial_metadata()
        assert values(initial, "x-header") == ["h"]
        [peer] = values(initial, "x-peer")
        assert isinstance(peer, str) and peer.startswith("ipv4:127.0.0.1:")
        check_trailers(await call.trailing_metadata())


async def test_a_stream_sends_headers_and_trailers() -> None:
    async with TestClient(make_app()) as tc:
        stream: grpc.aio.UnaryStreamMultiCallable[bytes, bytes]
        stream = tc.channel.grpc_channel.unary_stream("/meta.v1.Meta/Many")
        call = stream(b"")
        assert [message async for message in call] == [b"", b""]
        assert values(await call.initial_metadata(), "x-header") == ["h"]
        check_trailers(await call.trailing_metadata())


async def test_a_failure_keeps_its_trailers() -> None:
    async with TestClient(make_app()) as tc:
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await unary(tc, "Refuse")(b"")
    assert (raised.value.code(), raised.value.details()) == (grpc.StatusCode.NOT_FOUND, "nope")
    check_trailers(raised.value.trailing_metadata())
