"""OpenAPI 3.1 description of an application's RPCs, for Swagger UI (`app.enable_docs`).

One operation per RPC, `POST /<package>.<Service>/<Method>` (the gRPC path). Bodies are the
JSON of the Pydantic models, described by their JSON Schema (constraints, defaults and
descriptions included); a stream is NDJSON, one object per line. A failed call answers a
`Status` object (code, name, message, `google.rpc` details) with the HTTP status of its gRPC
code (`HTTP_STATUS`). Generated from the services and models, never written by hand.
"""

import enum
from collections.abc import Iterable, Sequence
from typing import Any, Literal, get_args

from grpc import StatusCode
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from fastgrpc3.api.service import GrpcService
from fastgrpc3.schema import MethodDef

__all__ = ["HTTP_STATUS", "NDJSON", "documented_services", "openapi"]

NDJSON = "application/x-ndjson"

#: HTTP status of each gRPC code, as grpc-gateway maps them.
HTTP_STATUS: dict[StatusCode, int] = {
    StatusCode.OK: 200,
    StatusCode.CANCELLED: 499,
    StatusCode.UNKNOWN: 500,
    StatusCode.INVALID_ARGUMENT: 400,
    StatusCode.DEADLINE_EXCEEDED: 504,
    StatusCode.NOT_FOUND: 404,
    StatusCode.ALREADY_EXISTS: 409,
    StatusCode.PERMISSION_DENIED: 403,
    StatusCode.RESOURCE_EXHAUSTED: 429,
    StatusCode.FAILED_PRECONDITION: 400,
    StatusCode.ABORTED: 409,
    StatusCode.OUT_OF_RANGE: 400,
    StatusCode.UNIMPLEMENTED: 501,
    StatusCode.INTERNAL: 500,
    StatusCode.UNAVAILABLE: 503,
    StatusCode.DATA_LOSS: 500,
    StatusCode.UNAUTHENTICATED: 401,
}

_HIDDEN_PACKAGES = ("grpc.reflection.",)

_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "A failed call: its gRPC status, as `google.rpc.Status` in JSON.",
    "properties": {
        "code": {"type": "integer", "description": "gRPC status code."},
        "status": {"type": "string", "enum": [code.name for code in StatusCode]},
        "message": {"type": "string"},
        "details": {
            "type": "array",
            "description": "`google.rpc` details (`ErrorInfo`, `BadRequest`…), each with "
            "its `@type`.",
            "items": {
                "type": "object",
                "properties": {"@type": {"type": "string"}},
                "additionalProperties": True,
            },
        },
    },
    "required": ["code", "status", "message", "details"],
}


def documented_services(services: Iterable[GrpcService]) -> list[GrpcService]:
    """The services the docs show (not server reflection, which speaks raw descriptors)."""
    return [s for s in services if not s.package.startswith(_HIDDEN_PACKAGES)]


def openapi(
    services: Iterable[GrpcService],
    *,
    title: str = "",
    version: str = "1.0.0",
    metadata: Sequence[str] = (),
) -> dict[str, Any]:
    """The OpenAPI document of `services`; `metadata` are the gRPC metadata keys offered as
    request headers (`x-user`, `authorization`…)."""
    shown = documented_services(services)
    calls = [(service, method) for service in shown for method in service.service_def().methods]
    refs, schemas = _schemas(calls)
    paths: dict[str, Any] = {}
    for service, method in calls:
        operation = _operation(service, method, refs, metadata)
        paths[f"/{service.full_name}/{method.name}"] = {"post": operation}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title or "gRPC services",
            "version": version,
            "description": _DESCRIPTION,
        },
        "tags": [{"name": s.full_name, "description": s.doc or ""} for s in shown],
        "paths": paths,
        "components": {"schemas": {**schemas, "Status": _STATUS_SCHEMA}},
    }


_DESCRIPTION = """\
Each RPC is a `POST` to its gRPC path, with the JSON of its request model. Streams are
NDJSON: one JSON object per line. A failed call answers a `Status` object with the HTTP
status of its gRPC code (`NOT_FOUND` → 404, `PERMISSION_DENIED` → 403…); in a response
stream, an error after the first message arrives as a last line `{"error": Status}`.
"""

_Mode = Literal["validation", "serialization"]
_Refs = dict[tuple[type[BaseModel], _Mode], dict[str, Any]]


def _schemas(calls: list[tuple[GrpcService, MethodDef]]) -> tuple[_Refs, dict[str, Any]]:
    """JSON Schema of every request (as validated) and response (as serialized)."""
    wanted: list[tuple[type[BaseModel], _Mode]] = []
    for _, method in calls:
        pairs: tuple[tuple[type[BaseModel], _Mode], ...] = (
            (method.request, "validation"),
            (method.response, "serialization"),
        )
        wanted += [pair for pair in pairs if pair not in wanted]
    refs, definitions = models_json_schema(wanted, ref_template="#/components/schemas/{model}")
    schemas: dict[str, Any] = definitions.get("$defs", {})
    for cls in _enums(model for model, _ in wanted):
        schema = schemas.get(cls.__name__)
        if schema is not None and "enum" in schema:
            _name_values(schema, cls)
    return {(model, mode): ref for (model, mode), ref in refs.items()}, schemas  # type: ignore[misc]


def _enums(models: Iterable[type[BaseModel]]) -> list[type[enum.Enum]]:
    """The enum classes used by `models` and the models they hold."""
    found: list[type[enum.Enum]] = []
    seen: set[type[BaseModel]] = set()
    pending = list(models)
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        types = [f.annotation for f in model.model_fields.values()]
        while types:
            annotation = types.pop()
            types += get_args(annotation)
            if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
                if annotation not in found:
                    found.append(annotation)
            elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
                pending.append(annotation)
    return found


def _name_values(schema: dict[str, Any], cls: type[enum.Enum]) -> None:
    """Show the member names of an enum whose JSON values are numbers."""
    members = [member for member in cls if member.value in schema["enum"]]
    names = ", ".join(f"`{member.value}` {member.name}" for member in members)
    description = schema.get("description", "")
    schema["description"] = f"{description}\n\n{names}" if description else names
    schema["x-enum-varnames"] = [member.name for member in members]


def _operation(
    service: GrpcService, method: MethodDef, refs: _Refs, metadata: Sequence[str]
) -> dict[str, Any]:
    request = refs[(method.request, "validation")]
    response = refs[(method.response, "serialization")]
    request_type = NDJSON if method.client_streaming else "application/json"
    response_type = NDJSON if method.server_streaming else "application/json"
    notes = []
    if method.client_streaming:
        notes.append(f"Request stream: NDJSON, one `{method.request.__name__}` per line.")
    if method.server_streaming:
        notes.append(f"Response stream: NDJSON, one `{method.response.__name__}` per line.")
    description = "\n\n".join(part for part in (method.doc, *notes) if part)
    status_body = {"application/json": {"schema": {"$ref": "#/components/schemas/Status"}}}
    return {
        "tags": [service.full_name],
        "operationId": f"{service.full_name}.{method.name}",
        "summary": method.name,
        "description": description,
        "parameters": [
            {
                "name": key,
                "in": "header",
                "required": False,
                "description": "Sent as gRPC metadata.",
                "schema": {"type": "string"},
            }
            for key in metadata
        ],
        "requestBody": {"required": True, "content": {request_type: {"schema": request}}},
        "responses": {
            "200": {"description": "OK", "content": {response_type: {"schema": response}}},
            "default": {"description": "The call failed", "content": status_body},
        },
    }
