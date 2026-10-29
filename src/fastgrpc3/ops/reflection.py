"""gRPC server reflection, `grpc.reflection.v1` and `v1alpha` (what `grpcurl` uses).

The files served are the `FileDescriptorProto`s of the compiled contract, as compiled: what a
client sees is exactly what the server runs. A file comes with its dependencies. proto3 has
no extensions, so extension queries find nothing.
"""

from collections.abc import AsyncIterator, Callable
from types import ModuleType
from typing import Any

from fastgrpc3._proto.grpc.reflection.v1 import reflection_models as v1
from fastgrpc3._proto.grpc.reflection.v1 import reflection_service as v1_service
from fastgrpc3._proto.grpc.reflection.v1alpha import reflection_models as v1alpha
from fastgrpc3._proto.grpc.reflection.v1alpha import reflection_service as v1alpha_service
from fastgrpc3.api.service import GrpcService
from fastgrpc3.schema import Schema

__all__ = ["reflection_services"]

NOT_FOUND = 5
INVALID_ARGUMENT = 3


def reflection_services(schema: Callable[[], Schema | None]) -> list[GrpcService]:
    """The v1 and v1alpha reflection services; `schema` returns the app's compiled contract."""
    service = GrpcService.implement(v1_service.ServerReflection)
    alpha = GrpcService.implement(v1alpha_service.ServerReflection)

    @service.stream_stream("ServerReflectionInfo")
    async def info(
        requests: AsyncIterator[v1.ServerReflectionRequest],
    ) -> AsyncIterator[v1.ServerReflectionResponse]:
        async for request in requests:
            yield _respond(v1, _started(schema), request)

    @alpha.stream_stream("ServerReflectionInfo")
    async def info_alpha(
        requests: AsyncIterator[v1alpha.ServerReflectionRequest],
    ) -> AsyncIterator[v1alpha.ServerReflectionResponse]:
        async for request in requests:
            yield _respond(v1alpha, _started(schema), request)

    return [service, alpha]


def _started(schema: Callable[[], Schema | None]) -> Schema:
    current = schema()
    if current is None:  # handlers only run once the app has started
        raise RuntimeError("reflection called before the server started")
    return current


def _respond(models: ModuleType, schema: Schema, request: Any) -> Any:
    response: dict[str, Any] = {"valid_host": request.host, "original_request": request}
    view = schema.info
    if request.list_services is not None:
        names = [models.ServiceResponse(name=name) for name in sorted(view.services)]
        response["list_services_response"] = models.ListServiceResponse(service=names)
    elif request.file_by_filename is not None:
        name = request.file_by_filename
        response.update(_files(models, schema, name if name in view.files else None, name))
    elif request.file_containing_symbol is not None:
        symbol = request.file_containing_symbol
        response.update(_files(models, schema, view.file_containing_symbol(symbol), symbol))
    elif request.all_extension_numbers_of_type is not None:
        type_name = request.all_extension_numbers_of_type
        if type_name in view.messages:
            response["all_extension_numbers_response"] = models.ExtensionNumberResponse(
                base_type_name=type_name
            )
        else:
            response["error_response"] = _error(models, NOT_FOUND, f"unknown type {type_name}")
    elif request.file_containing_extension is not None:
        response["error_response"] = _error(models, NOT_FOUND, "proto3 has no extensions")
    else:
        response["error_response"] = _error(models, INVALID_ARGUMENT, "empty request")
    return models.ServerReflectionResponse(**response)


def _files(models: ModuleType, schema: Schema, file: str | None, wanted: str) -> dict[str, Any]:
    if file is None:
        return {"error_response": _error(models, NOT_FOUND, f"{wanted} not found")}
    names = [file, *schema.info.file_dependencies(file)]
    protos = [schema.info.file_descriptor_proto(name) for name in names]
    return {"file_descriptor_response": models.FileDescriptorResponse(file_descriptor_proto=protos)}


def _error(models: ModuleType, code: int, message: str) -> Any:
    return models.ErrorResponse(error_code=code, error_message=message)
