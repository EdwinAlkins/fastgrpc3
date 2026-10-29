"""Python names for proto elements: modules, classes, fields and enum members.

A proto name Python cannot use as is (a keyword, a `BaseModel` attribute, a name the
annotations of the module need) gets a trailing `_`; the field keeps its proto name through
`field(n, proto_name=...)`, so the wire never changes.
"""

import keyword
import re

from pydantic import BaseModel

__all__ = [
    "class_name",
    "enum_member_names",
    "field_name",
    "method_name",
    "module_parts",
    "screaming_snake",
]

# Names the generated annotations refer to: a field or class with one of them would shadow it.
ANNOTATION_NAMES = frozenset(
    {"bool", "bytes", "dict", "float", "int", "list", "str", "datetime", "Any", "JsonValue"}
)
_RESERVED_FIELDS = frozenset(dir(BaseModel)) | ANNOTATION_NAMES
_INVALID_CHARS = re.compile(r"\W")


def module_parts(proto_file: str) -> tuple[tuple[str, ...], str]:
    """Package directories and module stem of a `.proto` file (`a/b/c.proto` -> (a, b), c)."""
    *directories, file_name = proto_file.split("/")
    stem = file_name.removesuffix(".proto")
    return tuple(_identifier(part) for part in directories), _identifier(stem)


def class_name(proto_name: str, parents: tuple[str, ...] = ()) -> str:
    """Flat class name of a message or enum: `Outer.Inner` -> `Outer_Inner`."""
    name = "_".join((*parents, proto_name))
    return name + "_" if keyword.iskeyword(name) or name in ANNOTATION_NAMES else name


def field_name(proto_name: str, taken: set[str]) -> str:
    """Python attribute for a proto field, unique among `taken` (updated)."""
    name = proto_name
    if name.startswith("_"):
        # Pydantic treats `_x` as a private attribute, not a field.
        name = name.lstrip("_") or "field"
        name += "_"
    while keyword.iskeyword(name) or name in _RESERVED_FIELDS or name in taken:
        name += "_"
    taken.add(name)
    return name


def method_name(rpc_name: str, reserved: set[str]) -> str:
    """Client attribute for an RPC: `GetOrder` -> `get_order`; `reserved` (updated) holds the
    names already used, and those of the client base class."""
    name = screaming_snake(rpc_name).lower()
    while keyword.iskeyword(name) or name in reserved:
        name += "_"
    reserved.add(name)
    return name


def screaming_snake(name: str) -> str:
    """`OrderStatus` -> `ORDER_STATUS`, the prefix buf expects on enum values."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).upper()


def enum_member_names(enum_name: str, values: list[str]) -> tuple[list[str], str | None]:
    """Python member names, and the `prefix=` the class needs (None: the default one).

    The usual `ENUM_NAME_` prefix is stripped when every value has it and every stripped
    name is a valid member name; otherwise names are kept as written (`prefix=""`).
    """
    prefix = screaming_snake(enum_name) + "_"
    stripped = [value.removeprefix(prefix) for value in values]
    if all(value.startswith(prefix) for value in values) and _valid_members(stripped):
        return stripped, None
    if not _valid_members(values):
        raise ValueError(f"enum {enum_name}: value names are not valid Python members")
    return list(values), ""


def _valid_members(names: list[str]) -> bool:
    return len(set(names)) == len(names) and all(
        name.isidentifier() and not keyword.iskeyword(name) and not name.startswith("_")
        for name in names
    )


def _identifier(part: str) -> str:
    name = _INVALID_CHARS.sub("_", part)
    if not name or name[0].isdigit():
        name = "_" + name
    return name + "_" if keyword.iskeyword(name) else name
