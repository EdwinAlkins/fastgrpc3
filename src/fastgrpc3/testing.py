"""`TestClient`: run an app on a free local port and call it, in tests.

async with TestClient(app) as tc:
    orders = tc.client(OrdersClient)
    assert (await orders.get_order(request)).status == Status.PAID
"""

from typing import Self

from fastgrpc3.api.app import GrpcApp
from fastgrpc3.api.client import Channel, GrpcClient
from fastgrpc3.api.security import Insecure

__all__ = ["TestClient"]


class TestClient:
    """A started app and a channel to it (a real server on 127.0.0.1, not a fake)."""

    __test__ = False  # not a pytest test class

    def __init__(self, app: GrpcApp, *, default_timeout: float | None = 10.0) -> None:
        if not isinstance(app.tls, Insecure):
            raise ValueError("TestClient needs an app with tls=Insecure()")
        self.app = app
        self.default_timeout = default_timeout
        self._channel: Channel | None = None

    @property
    def channel(self) -> Channel:
        if self._channel is None:
            raise RuntimeError("use TestClient in `async with`")
        return self._channel

    def client[C: GrpcClient](self, client_class: type[C]) -> C:
        """A client of the app."""
        return client_class(self.channel)

    async def __aenter__(self) -> Self:
        self.app.address = "127.0.0.1:0"
        await self.app.start()
        target = f"127.0.0.1:{self.app.bound_port}"
        self._channel = Channel(target, tls=Insecure(), default_timeout=self.default_timeout)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            if self._channel is not None:
                await self._channel.close()
        finally:
            self._channel = None
            await self.app.stop(None)
