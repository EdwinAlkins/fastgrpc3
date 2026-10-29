"""`Metadata`: gRPC headers and trailers, an ordered multimap with lower-case keys.

Keys ending in `-bin` carry `bytes`, the others `str`.
"""

from collections.abc import Iterable, Iterator, Mapping

__all__ = ["Metadata", "MetadataLike", "as_pairs"]

Value = str | bytes
SENSITIVE_KEYS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"}
)


class Metadata:
    """Ordered `(key, value)` pairs; a key may appear several times."""

    def __init__(self, *pairs: tuple[str, Value]) -> None:
        self._pairs: list[tuple[str, Value]] = []
        for key, value in pairs:
            self.add(key, value)

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, Value]] | None) -> "Metadata":
        """Metadata from `grpc`'s pairs (`None` is empty)."""
        return cls(*(pairs or ()))

    def add(self, key: str, value: Value) -> None:
        """Append a pair; `-bin` keys take `bytes`, other keys `str`."""
        key = key.lower()
        if key.endswith("-bin") != isinstance(value, bytes):
            kind = "bytes" if key.endswith("-bin") else "str"
            raise TypeError(f"metadata key {key!r} takes a {kind} value")
        self._pairs.append((key, value))

    def get(self, key: str, default: Value | None = None) -> Value | None:
        """The first value of `key`."""
        key = key.lower()
        return next((v for k, v in self._pairs if k == key), default)

    def get_all(self, key: str) -> list[Value]:
        """Every value of `key`, in order."""
        key = key.lower()
        return [v for k, v in self._pairs if k == key]

    def pairs(self) -> tuple[tuple[str, Value], ...]:
        """The pairs, as `grpc` takes them."""
        return tuple(self._pairs)

    def __iter__(self) -> Iterator[tuple[str, Value]]:
        return iter(self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Metadata) and self._pairs == other._pairs

    def __hash__(self) -> int:
        return hash(tuple(self._pairs))

    def __repr__(self) -> str:
        shown = [(k, "<redacted>" if k in SENSITIVE_KEYS else v) for k, v in self._pairs]
        return f"Metadata({shown!r})"


MetadataLike = Metadata | Mapping[str, Value] | Iterable[tuple[str, Value]]


def as_pairs(metadata: MetadataLike | None) -> tuple[tuple[str, Value], ...]:
    """Pairs from `Metadata`, a mapping or an iterable of pairs (keys lower-cased)."""
    if metadata is None:
        return ()
    if isinstance(metadata, Metadata):
        return metadata.pairs()
    items = metadata.items() if isinstance(metadata, Mapping) else metadata
    return Metadata(*items).pairs()
