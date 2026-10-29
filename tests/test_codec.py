"""`MessageCodec`: bytes <-> models through the compiled schema."""

import datetime
import decimal
import enum
import uuid
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from fastgrpc3.schema import MethodDef, Schema, SchemaRegistry, ServiceDef
from fastgrpc3.types import Int32, ProtoEnum, ProtoModel, field, key, proto_enum


def schema_of(*models: type[BaseModel]) -> Schema:
    registry = SchemaRegistry()
    methods = tuple(MethodDef(f"Call{model.__name__}", model, model) for model in models)
    registry.add_service(ServiceDef("Shop", "shop.v1", methods))
    return Schema.from_registry(registry)


def round_trip[M: BaseModel](message: M) -> M:
    codec = schema_of(type(message)).codec(type(message))
    return codec.decode(codec.encode(message))


class Color(ProtoEnum):
    UNSPECIFIED = 0
    RED = 1


class Line(BaseModel):
    sku: str
    qty: Int32 = 0


class Order(BaseModel):
    number: str = ""
    color: Color = Color.UNSPECIFIED
    note: str | None = None
    lines: list[Line] = []
    counters: dict[str, int] = {}
    at: datetime.datetime | None = None
    payload: dict[str, Any] | None = None


def test_bytes_follow_proto3_rules() -> None:
    codec = schema_of(Order).codec(Order)
    assert codec.encode(Order()) == b""
    # `optional` keeps an empty string; field 1 "A", field 3 "".
    assert codec.encode(Order(number="A", note="")) == bytes.fromhex("0a01411a00")
    assert codec.encode(Order(lines=[Line(sku="s", qty=2)])) == bytes.fromhex("22050a01731002")


def test_direct_path_round_trip() -> None:
    order = Order(
        number="A",
        color=Color.RED,
        note="",
        lines=[Line(sku="s", qty=2)],
        counters={"k": 1},
        at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        payload={"a": [1.0, "x", None]},
    )
    assert round_trip(order) == order


def test_an_empty_struct_is_sent_as_an_empty_message() -> None:
    class Bag(BaseModel):
        payload: dict[str, Any] = {}

    codec = schema_of(Bag).codec(Bag)
    assert codec.encode(Bag()) == bytes.fromhex("0a00")
    assert codec.decode(b"") == Bag()


def test_absent_fields_decode_to_zero_values_not_python_defaults() -> None:
    class Item(BaseModel):
        qty: int = 1

    codec = schema_of(Item).codec(Item)
    assert codec.encode(Item(qty=0)) == b""
    assert codec.decode(b"") == Item(qty=0)


def test_unknown_enum_numbers_are_kept_by_open_enums() -> None:
    codec = schema_of(Order).codec(Order)
    decoded = codec.decode(bytes.fromhex("1007"))
    assert decoded.color == 7
    assert decoded.color.name == "UNRECOGNIZED_7"
    assert codec.encode(decoded) == bytes.fromhex("1007")


def test_naive_datetimes_are_refused() -> None:
    codec = schema_of(Order).codec(Order)
    with pytest.raises(ValueError, match="naive"):
        codec.encode(Order(at=datetime.datetime(2024, 1, 1)))


class Carried(BaseModel):
    price: decimal.Decimal = decimal.Decimal(0)
    ident: uuid.UUID | None = None
    grid: list[list[int]] = []
    maybe: list[int] | None = None
    either: int | str = 0


@pytest.mark.parametrize(
    "message",
    [
        Carried(),
        Carried(
            price=decimal.Decimal("1.50"),
            ident=uuid.UUID(int=5),
            grid=[[1, 2], []],
            maybe=[],
            either="x",
        ),
        Carried(maybe=None, either=3),
    ],
)
def test_json_carrier_round_trip(message: Carried) -> None:
    assert round_trip(message) == message


def test_json_carrier_is_a_json_string_on_the_wire() -> None:
    codec = schema_of(Carried).codec(Carried)
    encoded = codec.encode(Carried(price=decimal.Decimal("1.5")))
    assert encoded.startswith(bytes.fromhex("0a05") + b'"1.5"')


@proto_enum(NEW=0, PAID=1)
class Status(enum.Enum):
    NEW = "new"
    PAID = "paid"


class Converted(BaseModel):
    status: Status = Status.NEW
    history: list[Status] = []
    by_shop: dict[str, Status] = {}
    on: datetime.date | None = None
    dates: list[datetime.date] = []


def test_proto_enum_and_dates_are_converted() -> None:
    message = Converted(
        status=Status.PAID,
        history=[Status.NEW, Status.PAID],
        by_shop={"a": Status.PAID},
        on=datetime.date(2024, 2, 29),
        dates=[datetime.date(1, 1, 1)],
    )
    codec = schema_of(Converted).codec(Converted)
    assert codec.encode(Converted(status=Status.PAID)) == bytes.fromhex("0801")
    assert codec.decode(codec.encode(message)) == message


def test_unknown_number_of_a_proto_enum_is_refused() -> None:
    codec = schema_of(Converted).codec(Converted)
    with pytest.raises(ValueError, match="7 is not a value of enum Status"):
        codec.decode(bytes.fromhex("0807"))


def test_nested_and_recursive_models_with_converted_fields() -> None:
    class Node(BaseModel):
        price: decimal.Decimal = decimal.Decimal(0)
        children: "list[Node]" = []
        by_name: "dict[str, Node]" = {}
        parent: "Node | None" = None

    tree = Node(
        price=decimal.Decimal(1),
        children=[Node(price=decimal.Decimal(2))],
        by_name={"x": Node(price=decimal.Decimal(3))},
        parent=Node(children=[Node(price=decimal.Decimal(4))]),
    )
    assert round_trip(tree) == tree


def test_aliases_do_not_interfere_with_decoding() -> None:
    class Aliased(BaseModel):
        from_: str = Field(default="", alias="from")
        to: str = key(2, default="", alias="To")

    message = Aliased.model_validate({"from": "a", "To": "b"})
    assert round_trip(message) == message


def test_decoding_validates_the_model() -> None:
    class Strict(BaseModel):
        number: str = Field(min_length=1)

    codec = schema_of(Strict).codec(Strict)
    with pytest.raises(ValidationError):
        codec.decode(b"")


def test_oneof_round_trip() -> None:
    class Card(ProtoModel):
        number: str = ""

    class Payment(ProtoModel):
        card: Card | None = field(1, default=None, oneof="method")
        iban: str | None = field(2, default=None, oneof="method")

    for message in (Payment(card=Card(number="4")), Payment(iban=""), Payment()):
        assert round_trip(message) == message


def test_codec_of_an_unknown_model() -> None:
    class Elsewhere(BaseModel):
        a: int = 0

    with pytest.raises(KeyError, match="not part of this schema"):
        schema_of(Order).codec(Elsewhere)


def test_codecs_are_cached() -> None:
    schema = schema_of(Order)
    assert schema.codec(Order) is schema.codec(Order)
