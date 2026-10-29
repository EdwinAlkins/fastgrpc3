"""Scalar markers, field declarations, `ProtoModel`, `ProtoEnum` and `proto_enum`."""

import enum
from typing import Annotated

import pytest
from pydantic import BaseModel, ValidationError

from fastgrpc3.types import (
    Fixed32,
    Int32,
    ProtoEnum,
    ProtoField,
    ProtoModel,
    UInt64,
    field,
    key,
    proto_enum,
    proto_field_of,
)


class Bounded(BaseModel):
    small: Int32 = 0
    large: UInt64 = 0
    fixed: Fixed32 = 0


@pytest.mark.parametrize(
    ("name", "value"),
    [("small", 2**31), ("small", -(2**31) - 1), ("large", -1), ("large", 2**64), ("fixed", -1)],
)
def test_markers_check_the_bounds_of_their_proto_type(name: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Bounded.model_validate({name: value})


def test_markers_accept_the_extreme_values() -> None:
    Bounded(small=-(2**31), large=2**64 - 1, fixed=2**32 - 1)


def test_field_and_key_record_the_number_without_touching_the_json_schema() -> None:
    class Model(BaseModel):
        a: str = field(3, default="", description="doc")
        b: int = key(4, default=0)
        c: Annotated[str, ProtoField(5)] = ""

    numbers = {name: proto_field_of(info) for name, info in Model.model_fields.items()}
    assert numbers == {"a": ProtoField(3), "b": ProtoField(4), "c": ProtoField(5)}
    assert "3" not in str(Model.model_json_schema())
    assert Model.model_fields["a"].description == "doc"


def test_proto_model_class_arguments_are_not_inherited() -> None:
    class Parent(ProtoModel, proto="shop.v1.Parent"):
        pass

    class Child(Parent, package="shop.v2"):
        pass

    assert (Parent.__proto_name__, Parent.__proto_package__) == ("shop.v1.Parent", None)
    assert (Child.__proto_name__, Child.__proto_package__) == (None, "shop.v2")


def test_proto_model_forbids_unknown_fields() -> None:
    class Model(ProtoModel):
        a: int = 0

    with pytest.raises(ValidationError):
        Model.model_validate({"b": 1})


class Payment(ProtoModel):
    card: str | None = field(1, default=None, oneof="method")
    iban: str | None = field(2, default=None, oneof="method")
    amount: int = field(3, default=0)


def test_oneof_accepts_at_most_one_member() -> None:
    assert Payment.__proto_oneofs__ == {"method": ("card", "iban")}
    Payment(card="4111")
    Payment()
    with pytest.raises(ValidationError, match="oneof 'method' has several fields set"):
        Payment(card="4111", iban="FR76")


def test_models_without_oneof_skip_the_check() -> None:
    class Plain(ProtoModel):
        a: int = 0

    assert not hasattr(Plain, "_check_oneofs")
    assert Plain.__proto_oneofs__ == {}


def test_oneof_in_annotated_form_is_refused() -> None:
    with pytest.raises(TypeError, match=r"field\(n, oneof=...\)"):

        class Model(ProtoModel):
            a: Annotated[str | None, ProtoField(1, oneof="x")] = None


def test_proto_enum_is_open() -> None:
    class Status(ProtoEnum, proto="shop.v1.Status"):
        UNSPECIFIED = 0
        PAID = 1

    assert Status.__proto_name__ == "shop.v1.Status"
    assert Status(7).name == "UNRECOGNIZED_7"
    assert list(Status) == [Status.UNSPECIFIED, Status.PAID]


def test_proto_enum_decorator_numbers_every_member() -> None:
    @proto_enum(NEW=0, PAID=1)
    class Status(enum.Enum):
        NEW = "new"
        PAID = "paid"

    assert Status.__proto_numbers__ == {"NEW": 0, "PAID": 1}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("numbers", "message"),
    [
        ({"NEW": 0}, "missing"),
        ({"NEW": 0, "PAID": 1, "LOST": 2}, "unknown"),
        ({"NEW": 1, "PAID": 1}, "unique"),
        ({"NEW": 1, "PAID": 2}, "numbered 0"),
    ],
)
def test_proto_enum_decorator_rejects_bad_numbers(numbers: dict[str, int], message: str) -> None:
    class Status(enum.Enum):
        NEW = "new"
        PAID = "paid"

    with pytest.raises(TypeError, match=message):
        proto_enum(**numbers)(Status)
