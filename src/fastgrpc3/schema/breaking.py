"""Changes between two versions of a contract that break existing peers (`fastgrpc3 breaking`).

Both versions are compiled descriptors. The rules follow `buf breaking`'s WIRE category:

- **breaking**: an old peer can no longer talk to the new one. A message, enum, service or
  method removed; a field or enum value removed without reserving its number; a field
  whose wire type, cardinality or oneof changed; a reserved number reused; a method whose
  request, response or streaming changed.
- **warning**: the wire still works, but the JSON mapping (the docs gateway, other
  languages' JSON) or the generated code changes: a field or enum value renamed, `optional`
  added or removed.

Not seen here: Pydantic validation (a new field without a default is required by the model,
yet proto3 has no required fields), which only the models know.
"""

from dataclasses import dataclass
from typing import Literal

from protoruf import Schema as DescriptorView
from protoruf.schema import EnumInfo, FieldInfo, MessageInfo, ServiceInfo

__all__ = ["Finding", "breaking_changes"]

# Scalar kinds that share a wire encoding and read each other's values (as buf's WIRE rules).
_WIRE_GROUPS = (
    {"int32", "uint32", "int64", "uint64", "bool"},
    {"sint32", "sint64"},
    {"fixed32", "sfixed32"},
    {"fixed64", "sfixed64"},
    {"string", "bytes"},
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One change, at `where` (a full name, with the field or value when there is one)."""

    level: Literal["breaking", "warning"]
    where: str
    what: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.what}"


def breaking_changes(old: DescriptorView, new: DescriptorView) -> list[Finding]:
    """What changed from `old` to `new` that existing peers would notice, breaking first."""
    findings: list[Finding] = []
    for name, message in old.messages.items():
        if message.is_map_entry:
            continue
        if name not in new.messages:
            findings.append(Finding("breaking", name, "message removed"))
        else:
            findings += _message_changes(message, new.messages[name])
    for name, enum in old.enums.items():
        if name not in new.enums:
            findings.append(Finding("breaking", name, "enum removed"))
        else:
            findings += _enum_changes(enum, new.enums[name])
    for name, service in old.services.items():
        if name not in new.services:
            findings.append(Finding("breaking", name, "service removed"))
        else:
            findings += _service_changes(service, new.services[name])
    return sorted(findings, key=lambda f: f.level != "breaking")


def _message_changes(old: MessageInfo, new: MessageInfo) -> list[Finding]:
    findings = []
    new_fields = {field.number: field for field in new.fields}
    for field in old.fields:
        where = f"{old.full_name}.{field.name}"
        current = new_fields.get(field.number)
        if current is None:
            if not _reserved(field.number, new.reserved_ranges):
                what = f"field {field.number} removed without reserving its number"
                findings.append(Finding("breaking", where, what))
            continue
        findings += _field_changes(where, field, current)
    for field in new.fields:
        if field.number not in {f.number for f in old.fields} and _reserved(
            field.number, old.reserved_ranges
        ):
            what = f"reuses number {field.number}, reserved for a removed field"
            findings.append(Finding("breaking", f"{new.full_name}.{field.name}", what))
    return findings


def _field_changes(where: str, old: FieldInfo, new: FieldInfo) -> list[Finding]:
    findings = []
    if _shape(old) != _shape(new):
        what = f"cardinality changed from {_shape(old)} to {_shape(new)}"
        findings.append(Finding("breaking", where, what))
    elif not _same_wire_type(old, new):
        findings.append(
            Finding("breaking", where, f"type changed from {_type(old)} to {_type(new)}")
        )
    if old.oneof != new.oneof:
        what = f"oneof changed from {old.oneof or 'none'} to {new.oneof or 'none'}"
        findings.append(Finding("breaking", where, what))
    if old.name != new.name:
        what = f"field {old.number} renamed to {new.name} (wire-compatible; JSON and code change)"
        findings.append(Finding("warning", where, what))
    if old.proto3_optional != new.proto3_optional:
        change = "added" if new.proto3_optional else "removed"
        what = f"`optional` {change} (wire-compatible; presence and code change)"
        findings.append(Finding("warning", where, what))
    return findings


def _shape(field: FieldInfo) -> str:
    if field.is_map:
        return "map"
    return "repeated" if field.is_list else "singular"


def _type(field: FieldInfo) -> str:
    if field.is_map:
        return f"map<{field.map_key_kind}, {field.map_value_type_name or field.map_value_kind}>"
    return field.type_name or field.kind


def _same_wire_type(old: FieldInfo, new: FieldInfo) -> bool:
    if old.is_map:
        return (old.map_key_kind, old.map_value_type_name or old.map_value_kind) == (
            new.map_key_kind,
            new.map_value_type_name or new.map_value_kind,
        )
    if old.type_name or new.type_name:  # messages and enums: the same named type
        return old.type_name == new.type_name
    return old.kind == new.kind or any(
        old.kind in group and new.kind in group for group in _WIRE_GROUPS
    )


def _enum_changes(old: EnumInfo, new: EnumInfo) -> list[Finding]:
    findings = []
    new_names = {value.number: value.name for value in new.values}
    for value in old.values:
        where = f"{old.full_name}.{value.name}"
        if value.number not in new_names:
            if not _reserved(value.number, new.reserved_ranges):
                what = f"value {value.number} removed without reserving its number"
                findings.append(Finding("breaking", where, what))
        elif new_names[value.number] != value.name and value.name not in new_names.values():
            what = (
                f"value {value.number} renamed to {new_names[value.number]} "
                "(wire-compatible; JSON changes)"
            )
            findings.append(Finding("warning", where, what))
    return findings


def _service_changes(old: ServiceInfo, new: ServiceInfo) -> list[Finding]:
    findings = []
    new_methods = {method.name: method for method in new.methods}
    for method in old.methods:
        where = f"{old.full_name}/{method.name}"
        current = new_methods.get(method.name)
        if current is None:
            findings.append(Finding("breaking", where, "method removed"))
            continue
        for side, before, after in (
            ("request", method.input_type, current.input_type),
            ("response", method.output_type, current.output_type),
        ):
            if before != after:
                findings.append(
                    Finding("breaking", where, f"{side} changed from {before} to {after}")
                )
        for side, streamed, streams in (
            ("client", method.client_streaming, current.client_streaming),
            ("server", method.server_streaming, current.server_streaming),
        ):
            if streamed != streams:
                state = "now streams" if streams else "no longer streams"
                findings.append(Finding("breaking", where, f"{side} side {state}"))
    return findings


def _reserved(number: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= number <= end for start, end in ranges)
