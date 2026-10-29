"""Compare resolved models and services with a compiled descriptor.

Used when generated models are bound to their embedded descriptor: a mismatch means the
generated modules are older than the descriptor (or were edited by hand).
"""

from protoruf import Schema as DescriptorView
from protoruf.schema import FieldInfo

from fastgrpc3.schema.annotations import FieldSpec
from fastgrpc3.schema.registry import ResolvedSchema

__all__ = ["mismatches"]

Shape = tuple[str, str, str, str | None]


def mismatches(schema: ResolvedSchema, view: DescriptorView) -> list[str]:
    """Differences between the models and the descriptor, one line each (empty: none)."""
    problems: list[str] = []
    for spec in schema.messages.values():
        message = view.messages.get(spec.full_name)
        if message is None:
            problems.append(f"message {spec.full_name} is not in the descriptor")
            continue
        ours = {f.number: _model_shape(schema, f) for f in spec.fields if f.number is not None}
        theirs = {f.number: _descriptor_shape(f) for f in message.fields}
        for number in sorted(ours.keys() | theirs.keys()):
            if ours.get(number) != theirs.get(number):
                problems.append(
                    f"{spec.full_name} field {number}: model {_describe(ours.get(number))}, "
                    f"descriptor {_describe(theirs.get(number))}"
                )
    for enum_spec in schema.enums.values():
        enum = view.enums.get(enum_spec.full_name)
        if enum is None:
            problems.append(f"enum {enum_spec.full_name} is not in the descriptor")
        elif set(enum_spec.values) != {(v.name, v.number) for v in enum.values}:
            problems.append(f"enum {enum_spec.full_name}: values differ")
    for service in schema.services:
        problems += _service_mismatches(schema, view, service.full_name)
    return problems


def _model_shape(schema: ResolvedSchema, spec: FieldSpec) -> Shape:
    label = "oneof" if spec.oneof else spec.label
    return spec.wire_name, label, schema.full_name(spec.value), spec.map_key


def _descriptor_shape(field: FieldInfo) -> Shape:
    if field.is_map:
        value = field.map_value_type_name or field.map_value_kind or ""
        return field.name, "map", value, field.map_key_kind
    if field.oneof:
        label = "oneof"
    elif field.is_list:
        label = "repeated"
    elif field.proto3_optional:
        label = "optional"
    else:
        label = "singular"
    return field.name, label, field.type_name or field.kind, None


def _describe(shape: Shape | None) -> str:
    if shape is None:
        return "has none"
    name, label, type_name, key = shape
    type_text = f"map<{key}, {type_name}>" if key else type_name
    return f"{label} {type_text} {name}"


def _service_mismatches(schema: ResolvedSchema, view: DescriptorView, full_name: str) -> list[str]:
    service = next(s for s in schema.services if s.full_name == full_name)
    info = view.services.get(full_name)
    if info is None:
        return [f"service {full_name} is not in the descriptor"]
    ours = {
        m.name: (
            schema.messages[m.request].full_name,
            schema.messages[m.response].full_name,
            m.client_streaming,
            m.server_streaming,
        )
        for m in service.methods
    }
    theirs = {
        m.name: (m.input_type, m.output_type, m.client_streaming, m.server_streaming)
        for m in info.methods
    }
    if ours != theirs:
        return [f"service {full_name}: methods differ"]
    return []
