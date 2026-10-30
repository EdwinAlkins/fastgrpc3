"""The transports a `GrpcApp` serves on: `grpc.aio` (the default) and tonic, a Rust server
(`fastgrpc3[tonic]`, experimental).

Importing this package never loads the native extension.
"""

import importlib.util
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["CAPABILITIES", "Transport", "TransportCapabilities", "available_transports"]


class Transport(StrEnum):
    """A transport; its value is also accepted as a string (`"tonic"`)."""

    GRPC_AIO = "grpc_aio"
    TONIC = "tonic"


@dataclass(frozen=True)
class TransportCapabilities:
    """What a transport supports, each item covered by the tests of both transports."""

    unary: bool = True
    server_streaming: bool = True
    client_streaming: bool = True
    bidirectional_streaming: bool = True
    tls: bool = True
    mtls: bool = True
    compression_gzip: bool = True
    health: bool = True
    reflection: bool = True
    native_grpc_aio_interceptors: bool = False
    raw_grpc_options: bool = False


CAPABILITIES = {
    Transport.GRPC_AIO: TransportCapabilities(
        native_grpc_aio_interceptors=True, raw_grpc_options=True
    ),
    Transport.TONIC: TransportCapabilities(),
}


def available_transports() -> list[Transport]:
    """The installed transports. Cheap: the native extension is only looked for, not loaded;
    `GrpcApp.start` checks that it really loads."""
    tonic = importlib.util.find_spec("fastgrpc3_transport") is not None
    return [Transport.GRPC_AIO, *([Transport.TONIC] if tonic else [])]
