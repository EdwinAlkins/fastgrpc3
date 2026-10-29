"""Python annotation -> proto field shape (table of `docs/conception/04`, decision D7)."""

import datetime
import decimal
import enum
import uuid
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, JsonValue, create_model

from fastgrpc3.schema.annotations import FieldSpec, classify_field
from fastgrpc3.types import (
    Fixed64,
    Float32,
    Int32,
    ProtoType,
    UInt32,
    Value,
    field,
    proto_enum,
)


class Sub(BaseModel):
    a: int = 0


class Color(enum.IntEnum):
    RED = 0


@proto_enum(NEW=0)
class Status(enum.Enum):
    NEW = "new"


class Loose(enum.Enum):
    A = "a"


class Perm(enum.IntFlag):
    READ = 1


def classify(annotation: Any) -> FieldSpec:
    model = create_model("M", x=(annotation, ...))
    return classify_field("x", model.model_fields["x"])


def shape(spec: FieldSpec) -> tuple[str, str]:
    value = spec.value.name or spec.value.py.__name__
    if spec.label == "map":
        value = f"{spec.map_key}, {value}"
    return spec.label, value


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (str, ("singular", "string")),
        (bytes, ("singular", "bytes")),
        (bool, ("singular", "bool")),
        (int, ("singular", "int64")),
        (float, ("singular", "double")),
        (Int32, ("singular", "int32")),
        (Fixed64, ("singular", "fixed64")),
        (Float32, ("singular", "float")),
        (str | None, ("optional", "string")),
        (Int32 | None, ("optional", "int32")),
        (Color, ("singular", "Color")),
        (Color | None, ("optional", "Color")),
        (Status, ("singular", "Status")),
        (Sub, ("singular", "Sub")),
        (Sub | None, ("singular", "Sub")),
        (list[str], ("repeated", "string")),
        (list[UInt32], ("repeated", "uint32")),
        (list[Sub], ("repeated", "Sub")),
        (dict[str, int], ("map", "string, int64")),
        (dict[Int32, Sub], ("map", "int32, Sub")),
        (dict[bool, Color], ("map", "bool, Color")),
        (datetime.datetime, ("singular", "google.protobuf.Timestamp")),
        (datetime.datetime | None, ("singular", "google.protobuf.Timestamp")),
        (datetime.timedelta, ("singular", "google.protobuf.Duration")),
        (datetime.date, ("singular", "google.type.Date")),
        (dict[str, Any], ("singular", "google.protobuf.Struct")),
        (dict[str, JsonValue], ("singular", "google.protobuf.Struct")),
        (dict[str, Any] | None, ("singular", "google.protobuf.Struct")),
        (list[dict[str, Any]], ("repeated", "google.protobuf.Struct")),
        (Any, ("singular", "google.protobuf.Value")),
        (JsonValue, ("singular", "google.protobuf.Value")),
        (list[Any], ("repeated", "google.protobuf.Value")),
        (dict[str, Value], ("map", "string, google.protobuf.Value")),
    ],
)
def test_types_with_a_proto_equivalent(annotation: Any, expected: tuple[str, str]) -> None:
    spec = classify(annotation)
    assert not spec.carrier
    assert shape(spec) == expected


@pytest.mark.parametrize(
    "annotation",
    [
        decimal.Decimal,
        uuid.UUID,
        Literal["a", "b"],
        int | str,
        Sub | Color,
        list[list[int]],
        list[int] | None,
        list[int | None],
        dict[float, int],
        dict[str, list[int]],
        tuple[int, int],
        set[str],
        Loose,
        Perm,
        datetime.time,
    ],
)
def test_types_without_a_proto_equivalent_use_the_json_carrier(annotation: Any) -> None:
    spec = classify(annotation)
    assert spec.carrier
    assert shape(spec) == ("singular", "string")


def test_marker_on_the_wrong_python_type_is_refused() -> None:
    with pytest.raises(TypeError, match="does not apply"):
        classify(Annotated[int, ProtoType("float")])


def test_number_oneof_alias_and_description_are_kept() -> None:
    class Model(BaseModel):
        x: str | None = field(4, default=None, oneof="choice", alias="X", description="Doc.")
        y: str = Field(default="")

    spec = classify_field("x", Model.model_fields["x"])
    assert (spec.number, spec.oneof, spec.alias, spec.doc) == (4, "choice", "X", "Doc.")
    plain = classify_field("y", Model.model_fields["y"])
    assert (plain.number, plain.oneof, plain.alias, plain.doc) == (None, None, None, None)
