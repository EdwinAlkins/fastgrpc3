"""`Context`: what a handler can see and do about its call, besides the request."""

from typing import TYPE_CHECKING

from fastgrpc3.api.metadata import Metadata, MetadataLike, as_pairs

if TYPE_CHECKING:
    import grpc.aio

__all__ = ["Context"]


class Context:
    """The current call: method, metadata, deadline, peer, headers and trailers.

    A handler or a dependency receives it by declaring a parameter annotated `Context`.
    """

    __slots__ = ("_grpc", "_metadata", "method")

    def __init__(self, grpc_context: "grpc.aio.ServicerContext[object, object]", method: str):
        self._grpc = grpc_context
        self._metadata: Metadata | None = None
        #: Full method path, `/package.Service/Method`.
        self.method = method

    @property
    def metadata(self) -> Metadata:
        """The request metadata (headers)."""
        if self._metadata is None:
            self._metadata = Metadata.from_pairs(self._grpc.invocation_metadata())
        return self._metadata

    def time_remaining(self) -> float | None:
        """Seconds left before the call's deadline, or `None` without deadline."""
        return self._grpc.time_remaining()

    def peer(self) -> str:
        """Transport address of the caller (not an authenticated identity)."""
        return self._grpc.peer()

    def peer_identities(self) -> tuple[bytes, ...]:
        """Identities verified by the transport (mTLS), empty if none."""
        return tuple(self._grpc.peer_identities() or ())

    def peer_identity(self) -> bytes | None:
        """The single verified identity; refuse a peer with several."""
        identities = self.peer_identities()
        if len(identities) > 1:
            raise ValueError("the peer has several verified identities")
        return identities[0] if identities else None

    def auth_context(self) -> dict[str, list[bytes]]:
        """Authentication properties set by the transport."""
        return {key: list(values) for key, values in self._grpc.auth_context().items()}

    async def send_initial_metadata(self, metadata: MetadataLike) -> None:
        """Send response headers now, before the first message."""
        await self._grpc.send_initial_metadata(as_pairs(metadata))

    def set_trailing_metadata(self, metadata: MetadataLike) -> None:
        """Response trailers, sent with the status."""
        self._grpc.set_trailing_metadata(as_pairs(metadata))
