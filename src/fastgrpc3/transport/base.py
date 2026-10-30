"""What the runtime needs from a transport, and what it gives back.

A transport (`grpc.aio`, later tonic) receives the calls and hands each one to an `Invoker`
with a `TransportContext`; the invoker answers with the response bytes (`None` for a server
stream, written through `TransportContext.write`) or an `RpcFailure`, which the transport
turns into a status. The runtime never imports a transport.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from fastgrpc3.api.errors import StatusCode

if TYPE_CHECKING:
    from fastgrpc3.runtime.dispatch import Invoker

__all__ = ["Pairs", "RpcFailure", "TransportBackend", "TransportContext"]

Pairs = Sequence[tuple[str, str | bytes]]


class TransportContext(Protocol):
    """One call, as the transport sees it.

    The names are `grpc.aio.ServicerContext`'s, so the `grpc.aio` transport passes its own
    context as is: nothing is allocated per call.
    """

    def time_remaining(self) -> float | None: ...

    def invocation_metadata(self) -> Iterable[tuple[str, str | bytes]] | None: ...

    def peer(self) -> str: ...

    def peer_identities(self) -> Iterable[bytes] | None: ...

    def auth_context(self) -> Mapping[str, Iterable[bytes]]: ...

    async def send_initial_metadata(self, metadata: Pairs, /) -> None: ...

    def set_trailing_metadata(self, metadata: Pairs, /) -> None: ...

    async def write(self, message: bytes, /) -> None: ...


@dataclass(frozen=True, slots=True)
class RpcFailure:
    """How a call ends when it fails; `details` is the encoded `google.rpc.Status`, sent
    in the `grpc-status-details-bin` trailer."""

    code: StatusCode
    message: str
    details: bytes | None = None


class TransportBackend(Protocol):
    """Serves the methods of an app: `add_method` for each, then `start`, then `stop`."""

    def add_method(self, invoker: "Invoker") -> None: ...

    async def start(self) -> int:
        """Start serving; returns the bound port."""
        ...

    async def stop(self, grace: float | None) -> None: ...

    async def wait_for_termination(self) -> None: ...
