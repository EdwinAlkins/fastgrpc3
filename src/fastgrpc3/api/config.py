"""Channel and server settings as types: compression, size limits, keepalive, retries."""

import json
from dataclasses import dataclass

import grpc

__all__ = [
    "DEFAULT_MAX_CONCURRENT_RPCS",
    "DEFAULT_MAX_MESSAGE_BYTES",
    "DEFAULT_MAX_METADATA_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "Compression",
    "Keepalive",
    "Limits",
    "RetryPolicy",
    "channel_options",
]

DEFAULT_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_METADATA_BYTES = 16 * 1024
DEFAULT_MAX_CONCURRENT_RPCS = 1024
DEFAULT_TIMEOUT_SECONDS = 30.0

Compression = grpc.Compression


@dataclass(frozen=True)
class Keepalive:
    """HTTP/2 keepalive pings."""

    time_ms: int = 60_000
    timeout_ms: int = 20_000
    permit_without_calls: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    """Client retries, for an explicit list of idempotent methods (`"pkg.Service/Method"`)."""

    methods: tuple[str, ...]
    max_attempts: int = 3
    initial_backoff: str = "0.1s"
    max_backoff: str = "1s"
    backoff_multiplier: float = 2.0
    retryable_status_codes: tuple[str, ...] = ("UNAVAILABLE",)

    def __post_init__(self) -> None:
        if not self.methods:
            raise ValueError("a retry policy lists the idempotent methods it applies to")
        if not 2 <= self.max_attempts <= 5:
            raise ValueError("retry max_attempts is between 2 and 5")
        for method in self.methods:
            service, _, name = method.lstrip("/").rpartition("/")
            if not service or not name:
                raise ValueError(f"retry method must be 'package.Service/Method', not {method!r}")

    def service_config(self) -> str:
        """The gRPC service config JSON of the policy."""
        names = []
        for method in self.methods:
            service, _, name = method.lstrip("/").rpartition("/")
            names.append({"service": service, "method": name})
        policy = {
            "maxAttempts": self.max_attempts,
            "initialBackoff": self.initial_backoff,
            "maxBackoff": self.max_backoff,
            "backoffMultiplier": self.backoff_multiplier,
            "retryableStatusCodes": list(self.retryable_status_codes),
        }
        return json.dumps({"methodConfig": [{"name": names, "retryPolicy": policy}]})


@dataclass(frozen=True)
class Limits:
    """Message and metadata size limits, in bytes (`None`: gRPC's own default)."""

    max_send_bytes: int | None = DEFAULT_MAX_MESSAGE_BYTES
    max_recv_bytes: int | None = DEFAULT_MAX_MESSAGE_BYTES
    max_metadata_bytes: int | None = DEFAULT_MAX_METADATA_BYTES

    def __post_init__(self) -> None:
        for name in ("max_send_bytes", "max_recv_bytes", "max_metadata_bytes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive int or None")


def channel_options(
    limits: Limits,
    keepalive: Keepalive | None = None,
    retry: RetryPolicy | None = None,
) -> list[tuple[str, int | str]]:
    """`grpc` channel or server options for these settings."""
    options: list[tuple[str, int | str]] = []
    if limits.max_send_bytes is not None:
        options.append(("grpc.max_send_message_length", limits.max_send_bytes))
    if limits.max_recv_bytes is not None:
        options.append(("grpc.max_receive_message_length", limits.max_recv_bytes))
    if limits.max_metadata_bytes is not None:
        options.append(("grpc.max_metadata_size", limits.max_metadata_bytes))
    if keepalive is not None:
        options += [
            ("grpc.keepalive_time_ms", keepalive.time_ms),
            ("grpc.keepalive_timeout_ms", keepalive.timeout_ms),
            ("grpc.keepalive_permit_without_calls", int(keepalive.permit_without_calls)),
        ]
    if retry is not None:
        options += [("grpc.enable_retries", 1), ("grpc.service_config", retry.service_config())]
    return options
