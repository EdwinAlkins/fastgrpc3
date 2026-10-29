"""`MessageCodec`: protobuf bytes <-> a Pydantic model, through protoruf's `Codec`.

The Rust codec handles everything the contract maps to proto, field names that differ
from the proto ones and aliases included. Only a model whose tree holds a value it does not
know (a JSON carrier, an `Enum` numbered by `proto_enum`) takes a slower path: the codec
works on dicts, converted here field by field.

`validate=False` builds the decoded models without Pydantic validation (`model_construct`
semantics, done in Rust): values have their proto types, but constraints and validators of
the models do not run. For trusted traffic only; models on the slower path are still
validated.
"""

import enum
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from protoruf import Codec
from pydantic import BaseModel

from fastgrpc3.schema.annotations import FieldSpec, TypeRef
from fastgrpc3.schema.registry import MessageSpec, ResolvedSchema

__all__ = ["CodecTables", "MessageCodec"]

Convert = Callable[[Any], Any]


@dataclass(frozen=True, slots=True)
class _Plan:
    """Fields of one message that need converting: `(name, to wire, from wire)`, and
    JSON carriers."""

    converters: tuple[tuple[str, Convert, Convert], ...]
    carriers: frozenset[str]


_Plans = dict[type[BaseModel], _Plan]


@dataclass(frozen=True, slots=True)
class CodecTables:
    """What every codec of a schema shares: the fields whose Python name differs from the
    proto one, and the conversion plans. Computed once per schema."""

    field_names: dict[str, dict[int, str]]
    plans: _Plans

    @classmethod
    def of(cls, schema: ResolvedSchema) -> "CodecTables":
        return cls(_field_names(schema), _build_plans(schema))


class MessageCodec[M: BaseModel]:
    """Encode and decode one message type.

    `naive_datetime="utc"` reads a naive `datetime` as UTC instead of refusing it;
    `unknown_enum="error"` refuses enum numbers the schema does not declare.
    """

    def __init__(
        self,
        descriptor: bytes,
        schema: ResolvedSchema,
        model: type[M],
        *,
        unknown_enum: Literal["keep", "error"] = "keep",
        naive_datetime: Literal["error", "utc"] = "error",
        validate: bool = True,
        tables: CodecTables | None = None,
    ) -> None:
        tables = tables or CodecTables.of(schema)
        self.model = model
        #: Full proto name of the message.
        self.full_name = schema.messages[model].full_name
        self._plans = tables.plans
        self._direct = all(cls not in self._plans for cls in _reachable(schema, model))
        self._native = Codec(
            descriptor,
            self.full_name,
            model,
            mode="validate" if validate or not self._direct else "construct",
            field_names=tables.field_names,
            unknown_enum=unknown_enum,
            naive_datetime=naive_datetime,
        )

    def encode(self, message: M) -> bytes:
        """Serialize a model instance to protobuf bytes."""
        if self._direct:
            return self._native.encode(message)
        return self._native.encode(_to_wire(self._plans, message))

    def decode(self, data: bytes) -> M:
        """Parse protobuf bytes into a model instance (validated unless `validate=False`)."""
        if self._direct:
            decoded: M = self._native.decode(data)
            return decoded
        values = _from_wire(self._plans, self.model, self._native.decode_dict(data))
        return self.model.model_validate(values, by_name=True)


def _to_wire(plans: _Plans, message: BaseModel) -> dict[str, Any]:
    values = dict(message.__dict__)
    plan = plans.get(type(message))
    if plan is None:
        return values
    for name, to_wire, _ in plan.converters:
        if values.get(name) is not None:
            values[name] = to_wire(values[name])
    if plan.carriers:
        dumped = message.model_dump(mode="json", include=set(plan.carriers))
        for name in plan.carriers:
            values[name] = json.dumps(dumped[name], separators=(",", ":"))
    return values


def _from_wire(plans: _Plans, model: type[BaseModel], values: dict[str, Any]) -> dict[str, Any]:
    plan = plans.get(model)
    if plan is None:
        return values
    for name, _, from_wire in plan.converters:
        if values.get(name) is not None:
            values[name] = from_wire(values[name])
    for name in plan.carriers:
        text = values.pop(name, "")
        # Proto3 omits an empty string; a carrier is never empty, so "" means absent.
        if text:
            values[name] = json.loads(text)
    return values


def _field_names(schema: ResolvedSchema) -> dict[str, dict[int, str]]:
    """Python names of the fields whose proto name differs, for protoruf."""
    names: dict[str, dict[int, str]] = {}
    for spec in schema.messages.values():
        renamed = {f.number: f.name for f in spec.fields if f.proto_name and f.number}
        if renamed:
            names[spec.full_name] = renamed
    return names


def _build_plans(schema: ResolvedSchema) -> _Plans:
    """Conversion plans of the messages that cannot go straight through the codec."""
    needs = _needs_conversion(schema)
    plans: _Plans = {}
    for model in needs:
        spec = schema.messages[model]
        converters = []
        for field_spec in spec.fields:
            if field_spec.carrier:
                continue
            pair = _value_converters(field_spec.value, needs, plans)
            if pair is not None:
                converters.append((field_spec.name, *_lift(field_spec, *pair)))
        carriers = frozenset(f.name for f in spec.fields if f.carrier)
        plans[model] = _Plan(tuple(converters), carriers)
    return plans


def _needs_conversion(schema: ResolvedSchema) -> set[type[BaseModel]]:
    """Messages with a converted field, directly or through a nested message."""
    needs = {
        model
        for model, spec in schema.messages.items()
        if any(f.carrier or _is_converted_leaf(f.value) for f in spec.fields)
    }
    changed = True
    while changed:  # fixed point, for recursive models
        changed = False
        for model, spec in schema.messages.items():
            if model in needs:
                continue
            if any(f.value.kind == "message" and f.value.py in needs for f in spec.fields):
                needs.add(model)
                changed = True
    return needs


def _is_converted_leaf(ref: TypeRef) -> bool:
    return ref.kind == "enum" and hasattr(ref.py, "__proto_numbers__")


def _value_converters(
    ref: TypeRef, needs: set[type[BaseModel]], plans: _Plans
) -> tuple[Convert, Convert] | None:
    if ref.kind == "enum" and hasattr(ref.py, "__proto_numbers__"):
        return _enum_converters(ref.py)
    if ref.kind == "message" and ref.py in needs:
        model = ref.py
        # `plans` is still being filled; the closures read it at call time.
        return (
            lambda value: _to_wire(plans, value),
            lambda value: _from_wire(plans, model, value),
        )
    return None


def _lift(spec: FieldSpec, to_wire: Convert, from_wire: Convert) -> tuple[Convert, Convert]:
    """Apply element converters to every element of a `repeated` or `map` field."""
    if spec.label == "repeated":
        return (
            lambda values: [to_wire(v) for v in values],
            lambda values: [from_wire(v) for v in values],
        )
    if spec.label == "map":
        return (
            lambda values: {k: to_wire(v) for k, v in values.items()},
            lambda values: {k: from_wire(v) for k, v in values.items()},
        )
    return to_wire, from_wire


def _enum_converters(cls: type[enum.Enum]) -> tuple[Convert, Convert]:
    numbers: dict[str, int] = cls.__proto_numbers__  # type: ignore[attr-defined]
    members = {numbers[name]: member for name, member in cls.__members__.items()}

    def to_wire(value: Any) -> int:
        return numbers[cls(value).name]

    def from_wire(number: int) -> enum.Enum:
        try:
            return members[number]
        except KeyError:
            raise ValueError(f"{number} is not a value of enum {cls.__name__}") from None

    return to_wire, from_wire


def _reachable(schema: ResolvedSchema, root: type[BaseModel]) -> set[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    pending = [root]
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        spec: MessageSpec = schema.messages[model]
        pending += [f.value.py for f in spec.fields if f.value.kind == "message"]
    return seen
