"""The tonic transport (experimental): the Rust server of `fastgrpc3-transport`, whose calls
the invokers run on this asyncio loop.

tokio queues the calls and the request messages without the GIL and wakes the loop through
a socket; the loop takes them by batches (`fastgrpc3-transport/docs/ARCHITECTURE.md`).
Response streams wait while their bounded channel is full; request streams are read one
message per credit, given back as the handler consumes them. When tonic gives a call up
(client gone, deadline, closed connection, stop), its task is cancelled, as with `grpc.aio`.

`GrpcApp` refuses the `grpc` settings this transport does not support yet.
"""

import asyncio
import functools
import socket
import sys
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import fastgrpc3_transport

from fastgrpc3.api.config import Keepalive, Limits
from fastgrpc3.api.errors import GrpcError, StatusCode
from fastgrpc3.api.security import MTLS, TLS, Insecure, Security, read_pem
from fastgrpc3.runtime.dispatch import Invoker
from fastgrpc3.transport.base import Pairs, RpcFailure

if TYPE_CHECKING:
    from fastgrpc3_transport import Call

#: The integration contract this module was written for (`fastgrpc3_transport`'s).
TRANSPORT_API = 1
_detected = getattr(fastgrpc3_transport, "TRANSPORT_API_VERSION", None)
if _detected != TRANSPORT_API:
    raise RuntimeError(
        "Installed fastgrpc3-transport is incompatible with this version of fastgrpc3.\n\n"
        f"Required transport API: {TRANSPORT_API}\nDetected transport API: {_detected}"
    )

__all__ = ["TonicTransport"]

_KINDS = {
    (False, False): "unary",
    (False, True): "server_stream",
    (True, False): "client_stream",
    (True, True): "bidi_stream",
}
#: tokio's semaphore takes at most `usize::MAX >> 3` permits.
_UNLIMITED = sys.maxsize >> 3

#: Request messages, then `(None, code)` at the end of the stream (`0`: a clean end).
Inbox = asyncio.Queue[tuple[bytes | None, int]]


class TonicTransport:
    """A `TransportBackend` on tonic. Needs `fastgrpc3-transport`."""

    def __init__(
        self,
        address: str,
        *,
        security: Security,
        maximum_concurrent_rpcs: int | None,
        limits: Limits,
        compression: str | None = None,
        keepalive: Keepalive | None = None,
        reuse_port: bool = False,
        threads: int = 1,
        max_batch: int = 128,
    ) -> None:
        host, _, port = address.rpartition(":")
        self.address = address
        self._host, self._port = host.strip("[]"), int(port)
        self._server = fastgrpc3_transport.Server(
            threads=threads,
            max_concurrent_rpcs=maximum_concurrent_rpcs or _UNLIMITED,
            max_recv_bytes=limits.max_recv_bytes,
            max_send_bytes=limits.max_send_bytes,
            max_metadata_bytes=limits.max_metadata_bytes,
            compression=compression,
            keepalive=None
            if keepalive is None
            else (keepalive.time_ms / 1000, keepalive.timeout_ms / 1000),
            reuse_port=reuse_port,
            tls=None if isinstance(security, Insecure) else _tls(security),
        )
        #: Events taken per loop turn: a burst is served over several turns.
        self._max_batch = max_batch
        self._invokers: dict[str, Invoker] = {}
        #: The task of every call in Python, by call id.
        self._running: dict[int, asyncio.Task[None]] = {}
        self._writable: dict[int, asyncio.Future[None]] = {}
        self._inboxes: dict[int, Inbox] = {}
        #: Unary answers handed to tokio together (`reply_many`): one wake-up per batch.
        self._answers: list[tuple[Call, bytes]] = []
        self._flush_soon = False
        self._wake: socket.socket | None = None
        #: Reads the wake-up socket where the loop cannot watch it (Windows' Proactor).
        self._waker_task: asyncio.Task[None] | None = None
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
        try:
            self._loop.add_reader(fd, self._readable)
        except NotImplementedError:
            self._waker_task = self._loop.create_task(self._read_wake_ups(self._wake))
        return int(port)

    async def stop(self, grace: float | None) -> None:
        """Refuse new calls, let those in flight finish for `grace` seconds (the loop keeps
        serving them), then cancel the handlers left."""
        if self._loop is None or self._wake is None:
            return
        self._server.shutdown(grace or 0.0)
        while not self._server.finished():
            await asyncio.sleep(0.01)
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._waker_task is not None:
            self._waker_task.cancel()
            await asyncio.gather(self._waker_task, return_exceptions=True)
        else:
            self._loop.remove_reader(self._wake.fileno())
        self._server.join()
        self._wake.close()
        self._wake = None
        self._stopped.set()

    async def wait_for_termination(self) -> None:
        await self._stopped.wait()

    async def send(self, call: "Call", data: bytes) -> bool:
        """One response of a stream; waits while its channel is full. False once the client
        is gone."""
        assert self._loop is not None
        while True:
            state = call.try_send(data)
            if state == fastgrpc3_transport.SENT:
                return True
            if state == fastgrpc3_transport.CLOSED:
                return False
            waiter = self._loop.create_future()
            self._writable[call.id] = waiter
            call.when_writable()
            await waiter

    async def _read_wake_ups(self, wake: socket.socket) -> None:
        assert self._loop is not None
        while await self._loop.sock_recv(wake, 4096):
            self._pump()

    def _readable(self) -> None:
        assert self._wake is not None
        try:  # noqa: SIM105 - the hot path: cheaper than contextlib.suppress
            self._wake.recv(4096)  # one byte per wake-up: a single read drains them
        except BlockingIOError:
            pass
        self._pump()

    def _pump(self) -> None:
        assert self._loop is not None
        calls, writable, requests, cancelled, more = self._server.take(self._max_batch)
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
            # Run at once: a call that ends without suspending needs no scheduled task.
            task = asyncio.eager_task_factory(self._loop, self._run(call, invoker, inbox))
            if not task.done():
                self._running[call.id] = task
                task.add_done_callback(functools.partial(self._ended, call.id))
        for call_id, message, code in requests:
            inbox = self._inboxes.get(call_id)
            if inbox is not None:
                inbox.put_nowait((message, code))
        for call_id in cancelled:  # after the calls: one may be given up in the same batch
            running = self._running.get(call_id)
            if running is not None:
                running.cancel()
        self._flush()  # the calls that ended at once

    def _flush(self) -> None:
        self._flush_soon = False
        if self._answers:
            answers, self._answers = self._answers, []
            self._server.reply_many(answers)

    def _ended(self, call_id: int, task: asyncio.Task[None]) -> None:
        self._running.pop(call_id, None)

    async def _run(self, call: "Call", invoker: Invoker, inbox: Inbox | None) -> None:
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
            self._answers.append((call, result))
            if not self._flush_soon:  # for a call that ends outside `_pump`
                self._flush_soon = True
                self._loop.call_soon(self._flush)  # type: ignore[union-attr]


def _tls(security: TLS | MTLS) -> tuple[bytes, bytes, bytes | None, bool]:
    if security.cert is None or security.key is None:
        raise ValueError("a TLS server needs cert and key")
    ca = None if security.ca is None else read_pem(security.ca)
    return read_pem(security.cert), read_pem(security.key), ca, isinstance(security, MTLS)


async def _read(call: "Call", inbox: Inbox) -> AsyncIterator[bytes]:
    while True:
        message, code = await inbox.get()
        if message is None:
            if code == 0:
                return
            if code == StatusCode.CANCELLED.value[0]:
                # The client reset its stream; `grpc.aio` cancels the handler then.
                raise asyncio.CancelledError
            raise GrpcError(StatusCode.RESOURCE_EXHAUSTED, "received message larger than max")
        call.consumed()
        yield message


class _Context:
    """The `TransportContext` of a tonic call.

    A unary call sends its headers with its answer, not at `send_initial_metadata`.
    """

    __slots__ = ("_call", "_transport")

    def __init__(self, transport: TonicTransport, call: "Call") -> None:
        self._transport = transport
        self._call = call

    def time_remaining(self) -> float | None:
        return self._call.time_remaining()

    def invocation_metadata(self) -> Pairs:
        return self._call.metadata()

    def peer(self) -> str:
        return self._call.peer

    def peer_identities(self) -> list[bytes] | None:
        return self._call.peer_identities()

    def auth_context(self) -> dict[str, list[bytes]]:
        return self._call.auth_context()

    async def send_initial_metadata(self, metadata: Pairs, /) -> None:
        self._call.send_initial_metadata(list(metadata))

    def set_trailing_metadata(self, metadata: Pairs, /) -> None:
        self._call.set_trailing_metadata(list(metadata))

    async def write(self, message: bytes, /) -> None:
        if not await self._transport.send(self._call, message):
            raise asyncio.CancelledError  # the client is gone; `grpc.aio` cancels the handler
