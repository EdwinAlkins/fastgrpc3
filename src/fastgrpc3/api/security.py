"""Transport security, always explicit: `TLS`, `MTLS` or `Insecure`, never a default.

PEM arguments take a file path or the PEM bytes.
"""

from dataclasses import dataclass, field
from pathlib import Path

import grpc

__all__ = [
    "MTLS",
    "TLS",
    "BearerToken",
    "Insecure",
    "Security",
    "channel_credentials",
    "check_security",
    "read_pem",
    "server_credentials",
]

Pem = str | Path | bytes


@dataclass(frozen=True)
class Insecure:
    """Plain-text transport, chosen on purpose (local development, behind a mesh)."""


@dataclass(frozen=True)
class TLS:
    """Server: `cert` and `key`. Client: `ca` to verify the server (system roots if None)."""

    cert: Pem | None = None
    key: Pem | None = field(default=None, repr=False)
    ca: Pem | None = None

    def __post_init__(self) -> None:
        if (self.cert is None) != (self.key is None):
            raise ValueError("TLS cert and key go together")


@dataclass(frozen=True)
class MTLS:
    """Mutual TLS: both sides present a certificate signed by `ca`."""

    cert: Pem
    key: Pem = field(repr=False)
    ca: Pem


Security = TLS | MTLS | Insecure


@dataclass(frozen=True)
class BearerToken:
    """`authorization: Bearer <token>` on every call; only allowed on a secure channel."""

    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.token or "\r" in self.token or "\n" in self.token:
            raise ValueError("a bearer token is non-empty and has no line break")

    def __repr__(self) -> str:
        return "BearerToken(token=<redacted>)"


def check_security(security: object) -> Security:
    """Return `security` if it is a transport choice; refuse `None` and anything else."""
    if security is None:
        raise ValueError("choose the transport explicitly: TLS(...), MTLS(...) or Insecure()")
    if not isinstance(security, TLS | MTLS | Insecure):
        raise TypeError(f"transport must be TLS, MTLS or Insecure, not {type(security).__name__}")
    return security


def server_credentials(security: TLS | MTLS) -> grpc.ServerCredentials:
    """Credentials of a TLS server."""
    if security.cert is None or security.key is None:
        raise ValueError("a TLS server needs cert and key")
    return grpc.ssl_server_credentials(
        [(read_pem(security.key), read_pem(security.cert))],
        root_certificates=_read_optional(security.ca),
        require_client_auth=isinstance(security, MTLS),
    )


def channel_credentials(security: TLS | MTLS) -> grpc.ChannelCredentials:
    """Credentials of a TLS client channel."""
    if isinstance(security, MTLS):
        return grpc.ssl_channel_credentials(
            root_certificates=read_pem(security.ca),
            private_key=read_pem(security.key),
            certificate_chain=read_pem(security.cert),
        )
    return grpc.ssl_channel_credentials(root_certificates=_read_optional(security.ca))


def read_pem(pem: Pem) -> bytes:
    """The PEM bytes of a path or of bytes."""
    return pem if isinstance(pem, bytes) else Path(pem).read_bytes()


def _read_optional(pem: Pem | None) -> bytes | None:
    return None if pem is None else read_pem(pem)
