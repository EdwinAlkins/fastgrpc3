"""Read Pydantic annotations into `FieldSpec`: the proto shape of each model field.

Types with a proto equivalent map to it (see `docs/conception/04`); any other type is sent
as a JSON *carrier* in a `string` field (decision D7), as fastgrpc2 did.
"""

import datetime
import enum
import inspect
import types
import typing
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, JsonValue
from pydantic.fields import FieldInfo

from fastgrpc3.types import ProtoType, proto_field_of

__all__ = ["FieldSpec", "TypeRef", "classify_field", "model_doc"]

Kind = Literal["scalar", "enum", "message", "well_known"]
Label = Literal["singular", "optional", "repeated", "map"]

SCALAR_TYPES = frozenset(
    {
        "double", "float", "int32", "int64", "uint32", "uint64", "sint32", "sint64",
        "fixed32", "fixed64", "sfixed32", "sfixed64", "bool", "string", "bytes",
    }
)  # fmt: skip
MAP_KEY_TYPES = SCALAR_TYPES - {"double", "float", "bytes"}

# Python type -> (well-known message, file to import).
WELL_KNOWN: dict[Any, tuple[str, str]] = {
    datetime.datetime: ("google.protobuf.Timestamp", "google/protobuf/timestamp.proto"),
    datetime.timedelta: ("google.protobuf.Duration", "google/protobuf/duration.proto"),
    datetime.date: ("google.type.Date", "google/type/date.proto"),
}
STRUCT = ("google.protobuf.Struct", "google/protobuf/struct.proto")
VALUE = ("google.protobuf.Value", "google/protobuf/struct.proto")

# Well-known types named by a `ProtoType` marker (`StringValue`, `FieldMask`…) -> file.
MARKED_WELL_KNOWN = {
    **{
        f"google.protobuf.{name}Value": "google/protobuf/wrappers.proto"
        for name in ("Double", "Float", "Int64", "UInt64", "Int32", "UInt32", "Bool")
    },
    "google.protobuf.StringValue": "google/protobuf/wrappers.proto",
    "google.protobuf.BytesValue": "google/protobuf/wrappers.proto",
    "google.protobuf.ListValue": "google/protobuf/struct.proto",
    "google.protobuf.FieldMask": "google/protobuf/field_mask.proto",
}
# Standard messages modelled by a class of `fastgrpc3.types`: imported, never rendered.
STANDARD_MESSAGES = {
    "google.protobuf.Empty": "google/protobuf/empty.proto",
    "google.protobuf.Any": "google/protobuf/any.proto",
}

_PLAIN_SCALARS: dict[Any, str] = {
    bool: "bool",
    int: "int64",
    float: "double",
    str: "string",
    bytes: "bytes",
}


@dataclass(frozen=True, slots=True)
class TypeRef:
    """The proto type of one value: a scalar, an enum, a message or a well-known type.

    `name` is the scalar name (`"int32"`) or, for a well-known type, its full name. For an
    enum or a message, the full name is only known once packages are resolved, so `py` (the
    Python class) identifies it; `name` is empty.
    """

    kind: Kind
    name: str = ""
    py: Any = None
    import_path: str | None = None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Proto shape of a model field.

    `name` is the Python attribute; `proto_name` is set when the proto field name differs.
    `value` is the element type for `repeated`, and the value type for `map` (whose key
    scalar is `map_key`). A carrier field is a `string` holding the JSON of the value.
    """

    name: str
    number: int | None
    label: Label
    value: TypeRef
    map_key: str | None = None
    carrier: bool = False
    oneof: str | None = None
    alias: str | None = None
    doc: str | None = None
    proto_name: str | None = None

    @property
    def wire_name(self) -> str:
        """The field name in the `.proto` and in the codec's dicts."""
        return self.proto_name or self.name


_CARRIER = TypeRef("scalar", "string")


def classify_field(name: str, info: FieldInfo) -> FieldSpec:
    """Build the `FieldSpec` of a resolved Pydantic field (`Model.model_fields[name]`)."""
    marker = proto_field_of(info)
    number = marker.number if marker else None
    oneof = marker.oneof if marker else None
    # Pydantic moves the outer `Annotated` metadata (e.g. of `x: Int32`) to `info.metadata`.
    proto_type = next((m.name for m in info.metadata if isinstance(m, ProtoType)), None)
    common: dict[str, Any] = {
        "name": name,
        "number": number,
        "oneof": oneof,
        "alias": info.alias if info.alias != name else None,
        "doc": info.description,
        "proto_name": marker.proto_name if marker and marker.proto_name != name else None,
    }

    annotation, optional = _split_optional(info.annotation)
    ref = _singular(annotation, proto_type)
    if ref is not None:
        if optional and ref.kind in ("scalar", "enum"):
            return FieldSpec(label="optional", value=ref, **common)
        return FieldSpec(label="singular", value=ref, **common)
    if not optional:
        container = _container(annotation)
        if container is not None:
            label, value, map_key = container
            return FieldSpec(label=label, value=value, map_key=map_key, **common)
    return FieldSpec(label="singular", value=_CARRIER, carrier=True, **common)


def model_doc(cls: type) -> str | None:
    """The docstring written on the class itself (not inherited from `BaseModel`)."""
    doc = vars(cls).get("__doc__")
    return inspect.cleandoc(doc) if doc else None


def _unwrap(annotation: Any) -> tuple[Any, str | None]:
    """Strip `Annotated`, `NewType` and `type X = ...` aliases; keep a `ProtoType` marker."""
    proto_type = None
    while True:
        if get_origin(annotation) is Annotated:
            base, *extras = get_args(annotation)
            for extra in extras:
                if isinstance(extra, ProtoType):
                    proto_type = extra.name
            annotation = base
        elif isinstance(annotation, typing.NewType):
            annotation = annotation.__supertype__
        elif isinstance(annotation, typing.TypeAliasType) and annotation is not JsonValue:
            annotation = annotation.__value__
        else:
            return annotation, proto_type


def _split_optional(annotation: Any) -> tuple[Any, bool]:
    """`X | None` -> `(X, True)`; any other annotation -> `(annotation, False)`."""
    # An `Annotated` around the union itself carries no proto marker we use.
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        rest = [arg for arg in args if arg is not type(None)]
        if len(rest) == 1 and len(rest) < len(args):
            return rest[0], True
    return annotation, False


def _singular(annotation: Any, proto_type: str | None = None) -> TypeRef | None:
    """The `TypeRef` of a non-container annotation, or `None` if it has no proto type."""
    annotation, inner_type = _unwrap(annotation)
    proto_type = inner_type or proto_type
    if proto_type in MARKED_WELL_KNOWN:
        return TypeRef("well_known", proto_type, import_path=MARKED_WELL_KNOWN[proto_type])
    if annotation is Any or annotation is JsonValue:
        return TypeRef("well_known", VALUE[0], import_path=VALUE[1])
    if _is_struct(annotation):
        return TypeRef("well_known", STRUCT[0], import_path=STRUCT[1])
    if not isinstance(annotation, type):
        return None
    if annotation in WELL_KNOWN:
        full_name, path = WELL_KNOWN[annotation]
        return TypeRef("well_known", full_name, py=annotation, import_path=path)
    if issubclass(annotation, BaseModel):
        return TypeRef("message", py=annotation)
    if issubclass(annotation, enum.Enum):
        return _enum(annotation)
    if annotation in _PLAIN_SCALARS:
        return _scalar(annotation, proto_type)
    return None


def _scalar(annotation: type, proto_type: str | None) -> TypeRef | None:
    if proto_type is None:
        return TypeRef("scalar", _PLAIN_SCALARS[annotation])
    expected = float if proto_type in ("float", "double") else int
    if annotation is not expected:
        raise TypeError(f"proto type {proto_type!r} does not apply to {annotation.__name__}")
    return TypeRef("scalar", proto_type)


def _enum(cls: type[enum.Enum]) -> TypeRef | None:
    if issubclass(cls, enum.Flag):
        return None
    if issubclass(cls, int) or hasattr(cls, "__proto_numbers__"):
        return TypeRef("enum", py=cls)
    return None


def _is_struct(annotation: Any) -> bool:
    """`dict[str, Any]` or `dict[str, JsonValue]`: a `google.protobuf.Struct`."""
    if get_origin(annotation) is not dict:
        return False
    key_type, value_type = get_args(annotation)
    return key_type is str and _unwrap(value_type)[0] in (Any, JsonValue)


def _container(annotation: Any) -> tuple[Label, TypeRef, str | None] | None:
    """`list[X]` -> repeated, `dict[K, V]` -> map or `Struct`; `None` if not representable."""
    annotation, _ = _unwrap(annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list and len(args) == 1:
        element = _singular(args[0])
        return ("repeated", element, None) if element is not None else None
    if origin is dict and len(args) == 2:
        key_type, value_type = args
        key = _singular(key_type)
        value = _singular(value_type)
        if key is None or key.kind != "scalar" or key.name not in MAP_KEY_TYPES:
            return None
        if value is None:
            return None
        return "map", value, key.name
    return None
