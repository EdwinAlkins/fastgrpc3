"""`Schema`: the compiled contract of an application, with one codec per model."""

from dataclasses import dataclass, field
from typing import Any

import protoruf
from pydantic import BaseModel

from fastgrpc3.schema.codec import MessageCodec
from fastgrpc3.schema.lock import Lock, Unlocked
from fastgrpc3.schema.registry import ResolvedSchema, SchemaRegistry
from fastgrpc3.schema.render import render

__all__ = ["Schema"]


@dataclass
class Schema:
    """Rendered `.proto` files, the descriptor compiled from them, and codecs on demand."""

    resolved: ResolvedSchema
    files: dict[str, str]
    descriptor: bytes
    info: protoruf.Schema
    _codecs: dict[type[BaseModel], MessageCodec[Any]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_registry(
        cls, registry: SchemaRegistry, lock: Lock | None = None, unlocked: Unlocked = "assign"
    ) -> "Schema":
        """Resolve, render and compile a code-first contract."""
        resolved = registry.build(lock, unlocked)
        files = render(resolved)
        descriptor = protoruf.compile_protos(roots=list(files), sources=files)
        return cls(resolved, files, descriptor, protoruf.Schema(descriptor))

    def codec[M: BaseModel](self, model: type[M]) -> MessageCodec[M]:
        """The codec of a registered model, created once."""
        codec = self._codecs.get(model)
        if codec is None:
            if model not in self.resolved.messages:
                raise KeyError(f"{model.__qualname__} is not part of this schema")
            codec = MessageCodec(self.descriptor, self.resolved, model)
            self._codecs[model] = codec
        return codec
