"""Code-first contracts: Pydantic models and services -> `.proto` files -> descriptor."""

from fastgrpc3.schema.codec import MessageCodec
from fastgrpc3.schema.lock import Change, Lock, LockError
from fastgrpc3.schema.registry import MethodDef, SchemaError, SchemaRegistry, ServiceDef
from fastgrpc3.schema.render import render
from fastgrpc3.schema.schema import Schema

__all__ = [
    "Change",
    "Lock",
    "LockError",
    "MessageCodec",
    "MethodDef",
    "Schema",
    "SchemaError",
    "SchemaRegistry",
    "ServiceDef",
    "render",
]
