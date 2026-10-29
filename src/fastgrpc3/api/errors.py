"""`GrpcError` and rich error details (`google.rpc.Status` in `grpc-status-details-bin`).

Details are models; the standard ones of `google/rpc/error_details.proto` (`ErrorInfo`,
`BadRequest`…) are built in, others are declared with `register_detail`. On the wire each
detail is a `google.protobuf.Any` whose type URL carries the full proto name, so clients in
other languages read them too.
"""

import functools
from collections.abc import Iterable

from grpc import StatusCode
from pydantic import BaseModel

from fastgrpc3._proto.google.rpc import error_details_models as _details
from fastgrpc3._proto.google.rpc.status_models import Status
from fastgrpc3.schema import Schema, SchemaRegistry
from fastgrpc3.schema.codec import MessageCodec
from fastgrpc3.types import AnyMessage

__all__ = [
    "DETAILS_TRAILER",
    "BadRequest",
    "DebugInfo",
    "ErrorInfo",
    "FieldViolation",
    "GrpcError",
    "Help",
    "LocalizedMessage",
    "PreconditionFailure",
    "QuotaFailure",
    "RequestInfo",
    "ResourceInfo",
    "RetryInfo",
    "StatusCode",
    "decode_details",
    "encode_details",
    "register_detail",
]

DETAILS_TRAILER = "grpc-status-details-bin"
TYPE_URL_PREFIX = "type.googleapis.com/"

ErrorInfo = _details.ErrorInfo
RetryInfo = _details.RetryInfo
DebugInfo = _details.DebugInfo
QuotaFailure = _details.QuotaFailure
PreconditionFailure = _details.PreconditionFailure
BadRequest = _details.BadRequest
FieldViolation = _details.BadRequest_FieldViolation
RequestInfo = _details.RequestInfo
ResourceInfo = _details.ResourceInfo
Help = _details.Help
LocalizedMessage = _details.LocalizedMessage

_CODES = {code.value[0]: code for code in StatusCode}


class GrpcError(Exception):
    """An RPC failure: status code, message and optional detail models.

    Raised by a handler, it becomes the call's status; raised by a client, it carries the
    status the server sent. Unknown details are kept as `AnyMessage`.
    """

    def __init__(
        self, code: StatusCode, message: str = "", details: Iterable[BaseModel] = ()
    ) -> None:
        self.code = code
        self.message = message
        self.details = list(details)
        super().__init__(f"{code.name}: {message}" if message else code.name)

    def detail[M: BaseModel](self, model: type[M]) -> M | None:
        """The first detail of type `model`, if any."""
        return next((d for d in self.details if isinstance(d, model)), None)


_registered: dict[str, type[BaseModel]] = {}


def register_detail[M: type[BaseModel]](model: M) -> M:
    """Declare a model as an error detail, so that clients decode it (usable as decorator)."""
    full_name = getattr(model, "__proto_name__", None) or _codec(model).full_name
    _registered[full_name] = model
    return model


def encode_details(code: StatusCode, message: str, details: Iterable[BaseModel]) -> bytes:
    """`google.rpc.Status` bytes of an error, for the details trailer."""
    packed = []
    for detail in details:
        codec = _codec(type(detail))
        type_url = TYPE_URL_PREFIX + codec.full_name
        packed.append(AnyMessage(type_url=type_url, value=codec.encode(detail)))
    status = Status(code=code.value[0], message=message, details=packed)
    return _codec(Status).encode(status)


def decode_details(data: bytes) -> tuple[StatusCode, str, list[BaseModel]]:
    """Code, message and details of `google.rpc.Status` bytes."""
    status = _codec(Status).decode(data)
    details: list[BaseModel] = []
    for packed in status.details:
        model = _registered.get(packed.type_url.rpartition("/")[2])
        details.append(packed if model is None else _codec(model).decode(packed.value))
    return _CODES.get(status.code, StatusCode.UNKNOWN), status.message, details


@functools.cache
def _codec[M: BaseModel](model: type[M]) -> MessageCodec[M]:
    """Codec of a detail model, from a schema of its own (compiled once)."""
    registry = SchemaRegistry()
    registry.add_model(model)
    return Schema.from_registry(registry).codec(model)


for _model in (
    ErrorInfo,
    RetryInfo,
    DebugInfo,
    QuotaFailure,
    PreconditionFailure,
    BadRequest,
    RequestInfo,
    ResourceInfo,
    Help,
    LocalizedMessage,
):
    register_detail(_model)
