"""`MessageCodec`: bytes <-> models through the compiled schema."""

import datetime
import decimal
import enum
import time
import uuid
from typing import Any

import pytest
from protoruf import Codec
from pydantic import BaseModel, Field, ValidationError, create_model

from fastgrpc3.schema import MethodDef, Schema, SchemaRegistry, ServiceDef
from fastgrpc3.types import (
    AnyMessage,
    Empty,
    FieldMask,
    Int32,
    Int64Value,
    ListValue,
    ProtoEnum,
    ProtoModel,
    StringValue,
    field,
    key,
    proto_enum,
)


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


def test_decoding_without_validation() -> None:
    """`validate=False`: models built as by `model_construct`, nested ones included; the
    proto types are kept, the model's constraints are not checked."""

    class Strict(BaseModel):
        number: str = Field(min_length=1)
        lines: list[Line] = []

    schema = schema_of(Strict)
    trusting = schema.codec(Strict, validate=False)
    assert trusting is not schema.codec(Strict)
    assert trusting is schema.codec(Strict, validate=False)
    data = trusting.encode(Strict.model_construct(number="", lines=[Line(sku="a", qty=2)]))
    decoded = trusting.decode(data)
    assert decoded.number == "" and decoded.lines == [Line(sku="a", qty=2)]
    assert type(decoded.lines[0]) is Line
    with pytest.raises(ValidationError):
        schema.codec(Strict).decode(data)


def test_models_on_the_converted_path_are_always_validated() -> None:
    class Priced(BaseModel):
        price: decimal.Decimal = decimal.Decimal(0)  # a JSON carrier
        name: str = Field(default="x", min_length=1)

    codec = schema_of(Priced).codec(Priced, validate=False)
    assert codec.decode(codec.encode(Priced(price=decimal.Decimal("2.5")))).price == 2.5
    with pytest.raises(ValidationError):
        codec.decode(b"")  # name "" breaks min_length


def test_the_schema_is_built_in_linear_time() -> None:
    """Rendering and creating codecs used to be quadratic in the number of messages."""

    def build(count: int) -> float:
        models = [
            create_model(f"M{i}", **{f"f{j}": (int, 0) for j in range(5)})  # type: ignore[call-overload]
            for i in range(count)
        ]
        started = time.perf_counter()
        schema = schema_of(*models)
        for model in models:
            schema.codec(model)
        return time.perf_counter() - started

    build(20)  # warm up
    assert build(400) < 10 * build(100)  # linear: about 4x; quadratic was about 16x


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


def test_well_known_markers_and_proto_names() -> None:
    class Trip(ProtoModel):
        from_: str = field(1, default="", proto_name="from")
        coupon: StringValue | None = field(2, default=None)
        limit: Int64Value | None = field(3, default=None)
        mask: FieldMask | None = field(4, default=None)
        values: ListValue | None = field(5, default=None)
        nothing: Empty | None = field(6, default=None)
        packed: AnyMessage | None = field(7, default=None)

    schema = schema_of(Trip)
    codec = schema.codec(Trip)
    message = Trip(
        from_="Paris",
        coupon="",
        limit=0,
        mask=["a.b"],
        values=[1.0, None],
        nothing=Empty(),
        packed=AnyMessage(type_url="t", value=b"v"),
    )
    assert codec.decode(codec.encode(message)) == message
    assert codec.decode(codec.encode(Trip())) == Trip()
    wire = Codec(schema.descriptor, "shop.v1.Trip").decode_dict(codec.encode(message))
    assert wire["from"] == "Paris"
