"""Building blocks of a contract: scalar markers, field numbers, `ProtoModel`, `ProtoEnum`.

Python has a single `int` and a single `float`, protobuf has many. The `Annotated` markers
below pick the exact proto type and let Pydantic check the bounds::

    class Line(ProtoModel, proto="orders.v1.Line"):
        sku: str = field(1)
        qty: UInt32 = field(2, default=0)
"""

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

from protoruf import OpenIntEnum
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

__all__ = [
    "Double",
    "Fixed32",
    "Fixed64",
    "Float32",
    "Int32",
    "Int64",
    "ProtoEnum",
    "ProtoField",
    "ProtoModel",
    "ProtoType",
    "SFixed32",
    "SFixed64",
    "SInt32",
    "SInt64",
    "UInt32",
    "UInt64",
    "field",
    "key",
    "proto_enum",
]


@dataclass(frozen=True, slots=True)
class ProtoType:
    """Proto scalar type of an `int` or `float` annotation (`"int32"`, `"fixed64"`…)."""

    name: str


@dataclass(frozen=True, slots=True)
class ProtoField:
    """Field number and `oneof` group, for `Annotated[T, ProtoField(3)]`.

    `number=None` leaves the number to `proto.lock`.
    """

    number: int | None = None
    oneof: str | None = None


_INT32 = Field(ge=-(2**31), le=2**31 - 1)
_INT64 = Field(ge=-(2**63), le=2**63 - 1)
_UINT32 = Field(ge=0, le=2**32 - 1)
_UINT64 = Field(ge=0, le=2**64 - 1)

Int32 = Annotated[int, _INT32, ProtoType("int32")]
Int64 = Annotated[int, _INT64, ProtoType("int64")]
UInt32 = Annotated[int, _UINT32, ProtoType("uint32")]
UInt64 = Annotated[int, _UINT64, ProtoType("uint64")]
SInt32 = Annotated[int, _INT32, ProtoType("sint32")]
SInt64 = Annotated[int, _INT64, ProtoType("sint64")]
Fixed32 = Annotated[int, _UINT32, ProtoType("fixed32")]
Fixed64 = Annotated[int, _UINT64, ProtoType("fixed64")]
SFixed32 = Annotated[int, _INT32, ProtoType("sfixed32")]
SFixed64 = Annotated[int, _INT64, ProtoType("sfixed64")]
Float32 = Annotated[float, ProtoType("float")]
Double = Annotated[float, ProtoType("double")]


def field(number: int | None = None, *, oneof: str | None = None, **kwargs: Any) -> Any:
    """Declare a model field with its proto number; other arguments go to `pydantic.Field`.

    `number=None` leaves the number to `proto.lock`. `oneof="name"` puts the field in a
    `oneof` group; its type must then be `T | None` with a `None` default.
    """
    info: FieldInfo = Field(**kwargs)
    info.metadata.append(ProtoField(number, oneof))
    return info


def key(number: int, **kwargs: Any) -> Any:
    """Declare a field number like fastgrpc2's `key(n)`; same as `field(number)`."""
    return field(number, **kwargs)


class ProtoModel(BaseModel):
    """Base class for contract messages, recommended but not required.

    Class arguments: `proto="pkg.Name"` sets the full message name, `package="pkg"` only
    the package (the class name is kept). Without them, the message goes into the package
    of the service that uses it.
    """

    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)

    __proto_name__: ClassVar[str | None] = None
    __proto_package__: ClassVar[str | None] = None
    __proto_oneofs__: ClassVar[dict[str, tuple[str, ...]]] = {}

    def __init_subclass__(
        cls, *, proto: str | None = None, package: str | None = None, **kwargs: Any
    ) -> None:
        super().__init_subclass__(**kwargs)
        # Not inherited: a subclass is another message.
        cls.__proto_name__ = proto
        cls.__proto_package__ = package
        if _declares_oneof(vars(cls)):
            # Pydantic collects validators after `__init_subclass__`, so only the classes
            # that need it pay for the check.
            cls._check_oneofs = model_validator(mode="after")(_check_oneofs)  # type: ignore[attr-defined]

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        groups: dict[str, list[str]] = {}
        for name, info in cls.model_fields.items():
            marker = proto_field_of(info)
            if marker is not None and marker.oneof is not None:
                groups.setdefault(marker.oneof, []).append(name)
        cls.__proto_oneofs__ = {group: tuple(names) for group, names in groups.items()}
        if groups and not hasattr(cls, "_check_oneofs"):
            raise TypeError(
                f"{cls.__name__}: declare oneof members with field(n, oneof=...), "
                "the Annotated form is not detected"
            )


def proto_field_of(info: FieldInfo) -> ProtoField | None:
    """The `ProtoField` marker of a Pydantic field, if any."""
    for item in info.metadata:
        if isinstance(item, ProtoField):
            return item
    return None


def _declares_oneof(namespace: Mapping[str, Any]) -> bool:
    for value in namespace.values():
        if isinstance(value, FieldInfo):
            marker = proto_field_of(value)
            if marker is not None and marker.oneof is not None:
                return True
    return False


def _check_oneofs(self: ProtoModel) -> ProtoModel:
    for group, names in type(self).__proto_oneofs__.items():
        set_names = [name for name in names if getattr(self, name) is not None]
        if len(set_names) > 1:
            raise ValueError(f"oneof {group!r} has several fields set: {', '.join(set_names)}")
    return self


class ProtoEnum(OpenIntEnum):
    """Open proto3 enum: an unknown number decodes to an `UNRECOGNIZED_<n>` pseudo-member.

    `proto="pkg.Name"` sets the full enum name; without it, the enum goes into the package
    of the message that uses it. Members are numbered by their value; 0 is required.
    """

    __proto_name__: ClassVar[str | None] = None

    def __init_subclass__(cls, *, proto: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.__proto_name__ = proto


def proto_enum[E: type[enum.Enum]](**numbers: int) -> Callable[[E], E]:
    """Number the members of an `Enum` whose values are not ints, like in fastgrpc2.

    Every member must be numbered, numbers must be unique, and 0 is required::

        @proto_enum(NEW=0, PAID=1)
        class Status(enum.Enum):
            NEW = "new"
            PAID = "paid"
    """

    def decorate(cls: E) -> E:
        members = set(cls.__members__)
        if set(numbers) != members:
            missing = sorted(members - set(numbers))
            unknown = sorted(set(numbers) - members)
            raise TypeError(f"proto_enum on {cls.__name__}: missing {missing}, unknown {unknown}")
        if len(set(numbers.values())) != len(numbers):
            raise TypeError(f"proto_enum on {cls.__name__}: numbers must be unique")
        if 0 not in numbers.values():
            raise TypeError(f"proto_enum on {cls.__name__}: a member numbered 0 is required")
        cls.__proto_numbers__ = dict(numbers)  # type: ignore[attr-defined]
        return cls

    return decorate
