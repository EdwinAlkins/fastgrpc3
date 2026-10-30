"""The tonic transport (experimental): the Rust server of `fastgrpc3-transport`, whose calls
the invokers run on this asyncio loop.

tokio queues the calls and the request messages without the GIL and wakes the loop through
a socket; the loop takes them by batches (`fastgrpc3-transport/docs/ARCHITECTURE.md`).
Response streams wait while their bounded channel is full; request streams are read one
message per credit, given back as the handler consumes them.

Not supported yet: response metadata, peer, deadlines (lots 5 and 6 of the plan);
`GrpcApp` refuses TLS, compression and the other `grpc` settings with this transport.
"""

import asyncio
import socket
import sys
from collections.abc import AsyncIterator

from fastgrpc3_transport import CLOSED, SENT, Call, Server

from fastgrpc3.api.errors import StatusCode
from fastgrpc3.runtime.dispatch import Invoker
from fastgrpc3.transport.base import Pairs, RpcFailure

__all__ = ["TonicTransport"]

_KINDS = {
    (False, False): "unary",
    (False, True): "server_stream",
    (True, False): "client_stream",
    (True, True): "bidi_stream",
}
#: tokio's semaphore takes at most `usize::MAX >> 3` permits.
_UNLIMITED = sys.maxsize >> 3

Inbox = asyncio.Queue[tuple[bytes | None, bool]]


class TonicTransport:
    """A `TransportBackend` on tonic. Needs `fastgrpc3-transport`."""

    def __init__(
        self,
        address: str,
        *,
        maximum_concurrent_rpcs: int | None,
        threads: int = 1,
        max_batch: int = 128,
    ) -> None:
        host, _, port = address.rpartition(":")
        self.address = address
        self._host, self._port = host.strip("[]"), int(port)
        self._server = Server(
            threads=threads, max_concurrent_rpcs=maximum_concurrent_rpcs or _UNLIMITED
        )
        #: Events taken per loop turn: a burst is served over several turns.
        self._max_batch = max_batch
        self._invokers: dict[str, Invoker] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._writable: dict[int, asyncio.Future[None]] = {}
        self._inboxes: dict[int, Inbox] = {}
        self._wake: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = asyncio.Event()

    def add_method(self, invoker: Invoker) -> None:
        self._invokers[invoker.path] = invoker

    async def start(self) -> int:
        for path, invoker in self._invokers.items():
            method = invoker.handler.method
            self._server.add_route(path, _KINDS[method.client_streaming, method.server_streaming])
        try:
            port, fd = self._server.start_queue(self._port, self._host)
        except OSError as e:
            raise RuntimeError(f"cannot bind {self.address}") from e
        self._wake = socket.socket(fileno=fd)
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(fd, self._readable)
        return port

    async def stop(self, grace: float | None) -> None:
        """Refuse new calls, let those in flight finish for `grace` seconds (the loop keeps
        serving them), then cancel the handlers left."""
        if self._loop is None or self._wake is None:
            return
        self._server.shutdown(grace or 0.0)
        while not self._server.finished():
            await asyncio.sleep(0.01)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._loop.remove_reader(self._wake.fileno())
        self._server.join()
        self._wake.close()
        self._wake = None
        self._stopped.set()

    async def wait_for_termination(self) -> None:
        await self._stopped.wait()

    async def send(self, call: Call, data: bytes) -> bool:
        """One response of a stream; waits while its channel is full. False once the client
        is gone."""
        assert self._loop is not None
        while True:
            state = call.try_send(data)
            if state == SENT:
                return True
            if state == CLOSED:
                return False
            waiter = self._loop.create_future()
            self._writable[call.id] = waiter
            call.when_writable()
            await waiter

    def _readable(self) -> None:
        assert self._wake is not None
        try:
            while self._wake.recv(4096):
                pass
        except BlockingIOError:
            pass
        self._pump()

    def _pump(self) -> None:
        assert self._loop is not None
        calls, writable, requests, more = self._server.take(self._max_batch)
        if more:  # the socket is written again only once the queue has emptied
            self._loop.call_soon(self._pump)
        for call_id in writable:
            waiter = self._writable.pop(call_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
        for call in calls:
            invoker = self._invokers[call.path]
            inbox: Inbox | None = None
            if invoker.handler.method.client_streaming:
                # Before the messages of this batch, which come after their call.
                inbox = self._inboxes[call.id] = asyncio.Queue()
            task = self._loop.create_task(self._run(call, invoker, inbox))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        for call_id, message, clean in requests:
            inbox = self._inboxes.get(call_id)
            if inbox is not None:
                inbox.put_nowait((message, clean))

    async def _run(self, call: Call, invoker: Invoker, inbox: Inbox | None) -> None:
        request = call.body if inbox is None else _read(call, inbox)
        try:
            result = await invoker.call(request, _Context(self, call))
        except asyncio.CancelledError:
            call.fail(StatusCode.CANCELLED.value[0], "the call was cancelled")
            raise
        finally:
            self._inboxes.pop(call.id, None)
            self._writable.pop(call.id, None)
        if isinstance(result, RpcFailure):
            call.fail(result.code.value[0], result.message, result.details)
        elif result is None:
            call.finish()
        else:
            call.reply(result)


async def _read(call: Call, inbox: Inbox) -> AsyncIterator[bytes]:
    while True:
        message, clean = await inbox.get()
        if message is None:
            if clean:
                return
            # The client reset its stream; `grpc.aio` cancels the handler then.
            raise asyncio.CancelledError
        call.consumed()
        yield message


class _Context:
    """The `TransportContext` of a tonic call.

    shortcut: no response metadata, peer or deadline yet; lots 5 and 6 of the plan add them.
    """

    __slots__ = ("_call", "_transport")

    def __init__(self, transport: TonicTransport, call: Call) -> None:
        self._transport = transport
        self._call = call

    def time_remaining(self) -> float | None:
        return None

    def invocation_metadata(self) -> Pairs:
        return self._call.metadata()

    def peer(self) -> str:
        return ""

    def peer_identities(self) -> None:
        return None

    def auth_context(self) -> dict[str, list[bytes]]:
        return {}

    async def send_initial_metadata(self, metadata: Pairs, /) -> None:
        raise NotImplementedError("response metadata is not supported by the tonic transport yet")

    def set_trailing_metadata(self, metadata: Pairs, /) -> None:
        raise NotImplementedError("response metadata is not supported by the tonic transport yet")

    async def write(self, message: bytes, /) -> None:
        if not await self._transport.send(self._call, message):
            raise asyncio.CancelledError  # the client is gone; `grpc.aio` cancels the handler
