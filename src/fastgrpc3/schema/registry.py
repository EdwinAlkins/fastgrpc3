"""`SchemaRegistry`: the code-first contract, from Pydantic models and service declarations.

The registry only collects declarations. `build()` walks the models reachable from the
services, gives every message and enum its full name, numbers the fields with
`proto.lock`, and returns a `ResolvedSchema` ready to render as `.proto` files.
"""

import enum
import re
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from fastgrpc3.schema.annotations import FieldSpec, TypeRef, classify_field, model_doc
from fastgrpc3.schema.lock import Change, Lock, Unlocked, assign_numbers

__all__ = [
    "EnumSpec",
    "MessageSpec",
    "MethodDef",
    "ResolvedSchema",
    "SchemaError",
    "SchemaRegistry",
    "ServiceDef",
]

DEFAULT_PACKAGE = "fastgrpc3"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


class SchemaError(TypeError):
    """A model or service that cannot be expressed as a proto contract."""


@dataclass(frozen=True, slots=True)
class MethodDef:
    """One RPC: request and response models, and which sides stream."""

    name: str
    request: type[BaseModel]
    response: type[BaseModel]
    client_streaming: bool = False
    server_streaming: bool = False
    doc: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceDef:
    """A gRPC service declaration; its package is also the default one of its models."""

    name: str
    package: str
    methods: tuple[MethodDef, ...]
    doc: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.package}.{self.name}"


@dataclass(frozen=True, slots=True)
class EnumSpec:
    """A resolved enum: `values` are `(proto name, number)`, zero first."""

    full_name: str
    py: type[enum.Enum]
    values: tuple[tuple[str, int], ...]
    doc: str | None = None

    @property
    def package(self) -> str:
        return self.full_name.rpartition(".")[0]

    @property
    def name(self) -> str:
        return self.full_name.rpartition(".")[2]


@dataclass(frozen=True, slots=True)
class MessageSpec:
    """A resolved message: numbered fields, plus what the lock keeps reserved."""

    full_name: str
    py: type[BaseModel]
    fields: tuple[FieldSpec, ...]
    reserved: tuple[int, ...] = ()
    reserved_names: tuple[str, ...] = ()
    doc: str | None = None

    @property
    def package(self) -> str:
        return self.full_name.rpartition(".")[0]

    @property
    def name(self) -> str:
        return self.full_name.rpartition(".")[2]


@dataclass
class ResolvedSchema:
    """Everything needed to render and compile the contract, and the updated lock."""

    messages: dict[type[BaseModel], MessageSpec]
    enums: dict[type[enum.Enum], EnumSpec]
    services: tuple[ServiceDef, ...]
    lock: Lock
    changes: list[Change]

    def full_name(self, ref: TypeRef) -> str:
        """Full proto name of a field type (scalars have none)."""
        if ref.kind == "message":
            return self.messages[ref.py].full_name
        if ref.kind == "enum":
            return self.enums[ref.py].full_name
        return ref.name

    def packages(self) -> list[str]:
        """Packages in first-seen order: those of the services, then of the models."""
        seen = [service.package for service in self.services]
        seen += [spec.package for spec in self.messages.values()]
        seen += [spec.package for spec in self.enums.values()]
        return list(dict.fromkeys(seen))


@dataclass
class SchemaRegistry:
    """Collect services and standalone models, then `build()` the contract."""

    default_package: str = DEFAULT_PACKAGE
    services: list[ServiceDef] = field(default_factory=list)
    models: list[tuple[type[BaseModel], str | None]] = field(default_factory=list)

    def add_service(self, service: ServiceDef) -> None:
        """Register a service and, through its methods, the models it uses."""
        if any(s.full_name == service.full_name for s in self.services):
            raise SchemaError(f"service {service.full_name} registered twice")
        self.services.append(service)

    def add_model(self, model: type[BaseModel], package: str | None = None) -> None:
        """Register a model no service uses (e.g. an error detail)."""
        self.models.append((model, package))

    def build(self, lock: Lock | None = None, unlocked: Unlocked = "assign") -> ResolvedSchema:
        """Resolve names and numbers. `unlocked="error"` refuses to invent a number."""
        return _Resolver(lock or Lock(), unlocked).run(self)


class _Resolver:
    def __init__(self, lock: Lock, unlocked: Unlocked) -> None:
        self.previous = lock
        self.unlocked = unlocked
        self.names: dict[str, Any] = {}
        self.messages: dict[type[BaseModel], MessageSpec] = {}
        self.enums: dict[type[enum.Enum], EnumSpec] = {}
        self.lock = Lock()
        self.changes: list[Change] = []

    def run(self, registry: SchemaRegistry) -> ResolvedSchema:
        for service in registry.services:
            _check_package(service.package, f"service {service.name}")
            _check_identifier(service.name, "service")
            seen: set[str] = set()
            for method in service.methods:
                _check_identifier(method.name, f"method of {service.full_name}")
                if method.name in seen:
                    raise SchemaError(f"{service.full_name}: method {method.name} declared twice")
                seen.add(method.name)
                self.message(method.request, service.package)
                self.message(method.response, service.package)
        for model, package in registry.models:
            self.message(model, package or registry.default_package)
        for name in self.previous.messages.keys() - self.lock.messages.keys():
            self.changes.append(Change("message_removed", name))
        return ResolvedSchema(
            self.messages, self.enums, tuple(registry.services), self.lock, self.changes
        )

    def claim(self, full_name: str, py: Any) -> None:
        owner = self.names.setdefault(full_name, py)
        if owner is not py:
            raise SchemaError(
                f"{full_name} is both {_qualname(owner)} and {_qualname(py)}: "
                'give one of them proto="..."'
            )

    def message(self, model: type[BaseModel], package: str) -> None:
        if model in self.messages:
            self.check_same_package(model, self.messages[model].package, package)
            return
        if not issubclass(model, BaseModel):
            raise SchemaError(f"{_qualname(model)} is not a Pydantic model")
        full_name = _full_name(model, package)
        self.claim(full_name, model)
        model.model_rebuild()  # resolve forward references; no-op once complete
        specs = [classify_field(name, info) for name, info in model.model_fields.items()]
        for spec in specs:
            _check_identifier(spec.name, f"field of {full_name}")
            if spec.oneof is not None:
                _check_oneof(full_name, model, spec)
        entry, changes = assign_numbers(
            full_name,
            [(spec.name, spec.number) for spec in specs],
            self.previous.messages.get(full_name),
            self.unlocked,
        )
        self.lock.messages[full_name] = entry
        self.changes += changes
        numbered = tuple(replace(spec, number=entry.fields[spec.name]) for spec in specs)
        # Registered before its fields, so that recursive models terminate.
        self.messages[model] = MessageSpec(
            full_name,
            model,
            numbered,
            tuple(entry.reserved),
            tuple(entry.reserved_names),
            model_doc(model),
        )
        child_package = full_name.rpartition(".")[0]
        for spec in numbered:
            self.type_ref(spec.value, child_package)

    def type_ref(self, ref: TypeRef, package: str) -> None:
        if ref.kind == "message":
            self.message(ref.py, package)
        elif ref.kind == "enum":
            self.enum(ref.py, package)

    def enum(self, cls: type[enum.Enum], package: str) -> None:
        if cls in self.enums:
            self.check_same_package(cls, self.enums[cls].package, package)
            return
        full_name = _full_name(cls, package)
        self.claim(full_name, cls)
        self.enums[cls] = EnumSpec(full_name, cls, _enum_values(cls), model_doc(cls))

    def check_same_package(self, py: Any, resolved: str, package: str) -> None:
        explicit = getattr(py, "__proto_name__", None) or getattr(py, "__proto_package__", None)
        if not explicit and resolved != package:
            raise SchemaError(
                f"{_qualname(py)} is used from packages {resolved} and {package}: "
                'give it proto="pkg.Name" or package="pkg"'
            )


def _full_name(py: type, package: str) -> str:
    proto_name = getattr(py, "__proto_name__", None)
    if proto_name:
        _check_package(proto_name, _qualname(py))
        if "." not in proto_name:
            raise SchemaError(f'{_qualname(py)}: proto="{proto_name}" needs a package')
        return str(proto_name)
    package = getattr(py, "__proto_package__", None) or package
    _check_package(package, _qualname(py))
    _check_identifier(py.__name__, "message or enum name")
    return f"{package}.{py.__name__}"


def _enum_values(cls: type[enum.Enum]) -> tuple[tuple[str, int], ...]:
    numbers: dict[str, int] | None = getattr(cls, "__proto_numbers__", None)
    proto_names: dict[str, str] = getattr(cls, "__proto_names__", {})
    prefix = _screaming_snake(cls.__name__) + "_"
    values = []
    # `__members__` includes aliases (several names for one number).
    for name, member in cls.__members__.items():
        number = numbers[name] if numbers is not None else int(member.value)
        _check_identifier(name, f"member of {_qualname(cls)}")
        proto_name = proto_names.get(name) or _prefixed(prefix, name)
        values.append((proto_name, number))
    if not any(number == 0 for _, number in values):
        raise SchemaError(f"{_qualname(cls)}: proto3 enums need a member numbered 0")
    # proto3 requires the zero value first.
    zero = next(index for index, (_, number) in enumerate(values) if number == 0)
    return (values[zero], *values[:zero], *values[zero + 1 :])


def _prefixed(prefix: str, name: str) -> str:
    upper = name.upper()
    return upper if upper.startswith(prefix) else prefix + upper


def _screaming_snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).upper()


def _check_oneof(full_name: str, model: type[BaseModel], spec: FieldSpec) -> None:
    owner = f"{full_name}.{spec.name}"
    if not hasattr(model, "_check_oneofs"):
        raise SchemaError(f"{owner}: oneof needs a ProtoModel (it checks one member at most)")
    _check_identifier(spec.oneof or "", f"oneof of {full_name}")
    info = model.model_fields[spec.name]
    if spec.label in ("repeated", "map") or info.default is not None:
        raise SchemaError(f"{owner}: a oneof member must be `T | None` with a None default")


def _check_identifier(name: str, what: str) -> None:
    if not _IDENTIFIER.match(name):
        raise SchemaError(f"{what} {name!r} is not a valid proto identifier (ASCII only)")


def _check_package(name: str, owner: str) -> None:
    if not _PACKAGE.match(name):
        raise SchemaError(f"{owner}: {name!r} is not a valid proto package or name")


def _qualname(py: Any) -> str:
    return f"{py.__module__}.{py.__qualname__}"
