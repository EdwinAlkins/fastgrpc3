"""fastgrpc3: a FastAPI-like gRPC framework for Python, powered by protoruf."""

from fastgrpc3.api.app import GrpcApp
from fastgrpc3.api.client import Channel, GrpcClient
from fastgrpc3.api.config import Compression, Keepalive, RetryPolicy
from fastgrpc3.api.context import Context
from fastgrpc3.api.deps import Depends, blocking, non_blocking
from fastgrpc3.api.errors import (
    BadRequest,
    ErrorInfo,
    FieldViolation,
    GrpcError,
    StatusCode,
    register_detail,
)
from fastgrpc3.api.metadata import Metadata
from fastgrpc3.api.security import MTLS, TLS, BearerToken, Insecure
from fastgrpc3.api.service import GrpcService
from fastgrpc3.runtime.dispatch import CallNext
from fastgrpc3.schema import (
    Lock,
    LockError,
    MethodDef,
    Schema,
    SchemaError,
    SchemaRegistry,
    ServiceDef,
)
from fastgrpc3.types import ProtoEnum, ProtoField, ProtoModel, field, key, proto_enum

__version__ = "0.1.0.dev0"

__all__ = [
    "MTLS",
    "TLS",
    "BadRequest",
    "BearerToken",
    "CallNext",
    "Channel",
    "Compression",
    "Context",
    "Depends",
    "ErrorInfo",
    "FieldViolation",
    "GrpcApp",
    "GrpcClient",
    "GrpcError",
    "GrpcService",
    "Insecure",
    "Keepalive",
    "Lock",
    "LockError",
    "Metadata",
    "MethodDef",
    "ProtoEnum",
    "ProtoField",
    "ProtoModel",
    "RetryPolicy",
    "Schema",
    "SchemaError",
    "SchemaRegistry",
    "ServiceDef",
    "StatusCode",
    "__version__",
    "blocking",
    "field",
    "key",
    "non_blocking",
    "proto_enum",
    "register_detail",
]
