"""`Schema`: the compiled contract of an application, with one codec per model."""

from dataclasses import dataclass, field
from typing import Any

import protoruf
from pydantic import BaseModel

from fastgrpc3.schema.codec import CodecTables, MessageCodec
from fastgrpc3.schema.lock import Lock, Unlocked
from fastgrpc3.schema.registry import ResolvedSchema, SchemaError, SchemaRegistry
from fastgrpc3.schema.render import render
from fastgrpc3.schema.verify import mismatches

__all__ = ["Schema"]


@dataclass
class Schema:
    """Rendered `.proto` files, the descriptor compiled from them, and codecs on demand."""

    resolved: ResolvedSchema
    files: dict[str, str]
    descriptor: bytes
    info: protoruf.Schema
    _codecs: dict[tuple[type[BaseModel], bool], MessageCodec[Any]] = field(
        default_factory=dict, repr=False
    )
    _tables: CodecTables | None = field(default=None, repr=False)

    @classmethod
    def from_registry(
        cls, registry: SchemaRegistry, lock: Lock | None = None, unlocked: Unlocked = "assign"
    ) -> "Schema":
        """Resolve, render and compile a code-first contract."""
        resolved = registry.build(lock, unlocked)
        files = render(resolved)
        descriptor = protoruf.compile_protos(roots=list(files), sources=files)
        return cls(resolved, files, descriptor, protoruf.Schema(descriptor))

    @classmethod
    def from_descriptor(cls, registry: SchemaRegistry, descriptor: bytes) -> "Schema":
        """Bind models (generated ones) to a compiled descriptor, without compiling.

        Raises `SchemaError` if the models no longer match the descriptor.
        """
        resolved = registry.build(unlocked="error")
        info = protoruf.Schema(descriptor)
        problems = mismatches(resolved, info)
        if problems:
            details = "\n  ".join(problems)
            raise SchemaError(
                f"the models do not match the descriptor (run `fastgrpc3 generate`):\n  {details}"
            )
        return cls(resolved, {}, descriptor, info)

    def codec[M: BaseModel](self, model: type[M], *, validate: bool = True) -> MessageCodec[M]:
        """The codec of a registered model, created once (`validate=False`: decoded models
        are built without Pydantic validation, see `MessageCodec`)."""
        codec = self._codecs.get((model, validate))
        if codec is None:
            if model not in self.resolved.messages:
                raise KeyError(f"{model.__qualname__} is not part of this schema")
            if self._tables is None:
                self._tables = CodecTables.of(self.resolved)
            codec = MessageCodec(
                self.descriptor, self.resolved, model, validate=validate, tables=self._tables
            )
            self._codecs[(model, validate)] = codec
        return codec
