"""Cancellation and deadlines: the handler is cancelled, its dependencies cleaned up."""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from pydantic import BaseModel

from fastgrpc3 import Context, Depends, GrpcApp, GrpcClient, GrpcError, GrpcService, Insecure
from fastgrpc3.api.errors import StatusCode
from fastgrpc3.testing import TestClient

pytestmark = pytest.mark.usefixtures("transport")


class Req(BaseModel):
    n: int = 0


class Rep(BaseModel):
    n: int = 0


class LifeClient(GrpcClient):
    package = "life.v1"
    service = "Life"
    hang = GrpcClient.unary("Hang", Req, Rep)
    drip = GrpcClient.unary_stream("Drip", Req, Rep)
    gather = GrpcClient.stream_unary("Gather", Req, Rep)


class Life:
    """A service whose handlers wait forever, and what happened to them."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.started = asyncio.Event()
        self.remaining: list[float | None] = []

    def app(self) -> GrpcApp:
        events, started, remaining = self.events, self.started, self.remaining

        async def resource() -> AsyncIterator[str]:
            events.append("open")
            try:
                yield "resource"
            finally:
                events.append("close")

        async def forever() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("cancelled")
                raise

        service = GrpcService("Life", package="life.v1")

        @service.unary()
        async def Hang(req: Req, ctx: Context, _: str = Depends(resource)) -> Rep:
            remaining.append(ctx.time_remaining())
            await forever()
            return Rep()

        @service.unary_stream()
        async def Drip(req: Req, _: str = Depends(resource)) -> AsyncIterator[Rep]:
            yield Rep(n=1)
            await forever()

        @service.stream_unary()
        async def Gather(requests: AsyncIterator[Req], _: str = Depends(resource)) -> Rep:
            async for _request in requests:
                await forever()
            return Rep()

        app = GrpcApp(tls=Insecure())
        app.add_service(service)
        return app

    async def cleaned_up(self, app: GrpcApp, calls: int = 1) -> None:
        await until(lambda: self.events.count("close") == calls)
        assert self.events.count("cancelled") == calls
        # No call left in the tonic transport's registry.
        await until(lambda: not getattr(app._server, "_running", None))


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


async def test_a_deadline_cancels_a_unary_handler() -> None:
    life = Life()
    async with TestClient(app := life.app()) as tc:
        with pytest.raises(GrpcError) as raised:
            await tc.client(LifeClient).hang(Req(), timeout=0.3)
        assert raised.value.code == StatusCode.DEADLINE_EXCEEDED
        await life.cleaned_up(app)
    [remaining] = life.remaining
    assert remaining is not None and 0 < remaining < 0.35  # gRPC rounds what it sends


async def test_a_client_that_cancels_cancels_the_handler() -> None:
    life = Life()
    async with TestClient(app := life.app()) as tc:
        call = asyncio.create_task(tc.client(LifeClient).hang(Req()))
        await life.started.wait()
        call.cancel()
        await life.cleaned_up(app)


async def test_a_deadline_ends_a_server_stream() -> None:
    life = Life()
    async with TestClient(app := life.app()) as tc:
        received = []
        with pytest.raises(GrpcError) as raised:
            async for response in tc.client(LifeClient).drip(Req(), timeout=0.3):
                received.append(response)
        assert received == [Rep(n=1)]
        assert raised.value.code == StatusCode.DEADLINE_EXCEEDED
        await life.cleaned_up(app)


async def test_a_deadline_ends_a_client_stream() -> None:
    life = Life()

    async def requests() -> AsyncIterator[Req]:
        yield Req()
        await asyncio.Event().wait()  # the client never ends its stream

    async with TestClient(app := life.app()) as tc:
        with pytest.raises(GrpcError) as raised:
            await tc.client(LifeClient).gather(requests(), timeout=0.3)
        assert raised.value.code == StatusCode.DEADLINE_EXCEEDED
        await life.cleaned_up(app)


async def test_many_streams_cut_by_their_deadline_leave_nothing_behind() -> None:
    life = Life()

    async def read(client: LifeClient) -> None:
        with pytest.raises(GrpcError):
            async for _ in client.drip(Req(), timeout=0.5):
                pass

    async with TestClient(app := life.app()) as tc:
        client = tc.client(LifeClient)
        await asyncio.gather(*(read(client) for _ in range(100)))
        await life.cleaned_up(app, calls=100)
