"""Standard health checking (`grpc.health.v1.Health`): `Check`, `Watch` and `List`.

    health = app.enable_health()      # every service SERVING once started, NOT_SERVING on stop
    health.set_not_serving("orders.v1.Orders")

The empty service name `""` is the server as a whole. `Check` of an unknown service fails
with `NOT_FOUND`; `Watch` of one streams `SERVICE_UNKNOWN` until it becomes known.
"""

import asyncio
from collections.abc import AsyncIterator

from fastgrpc3._proto.grpc.health.v1 import health_models as models
from fastgrpc3._proto.grpc.health.v1.health_service import Health
from fastgrpc3.api.errors import GrpcError, StatusCode
from fastgrpc3.api.service import GrpcService

__all__ = ["HealthService", "ServingStatus"]

ServingStatus = models.HealthCheckResponse_ServingStatus


class HealthService:
    """Serving status per service name, and the gRPC service that reports it."""

    def __init__(self) -> None:
        self._statuses: dict[str, ServingStatus] = {}
        self._watchers: dict[str, set[asyncio.Queue[ServingStatus]]] = {}
        self.service = self._build_service()

    def status(self, service: str = "") -> ServingStatus | None:
        """The status of a service, or `None` if unknown."""
        return self._statuses.get(service)

    def set_status(self, service: str, status: ServingStatus) -> None:
        """Set the status of a service (`""`: the whole server) and notify watchers."""
        self._statuses[service] = status
        for queue in self._watchers.get(service, ()):
            queue.put_nowait(status)

    def set_serving(self, service: str = "") -> None:
        self.set_status(service, ServingStatus.SERVING)

    def set_not_serving(self, service: str = "") -> None:
        self.set_status(service, ServingStatus.NOT_SERVING)

    def set_all(self, status: ServingStatus) -> None:
        """Set every known service, and the server, to `status`."""
        for service in [*self._statuses, ""]:
            self.set_status(service, status)

    def _build_service(self) -> GrpcService:
        service = GrpcService.implement(Health)

        @service.unary("Check")
        async def check(request: models.HealthCheckRequest) -> models.HealthCheckResponse:
            status = self._statuses.get(request.service)
            if status is None:
                raise GrpcError(StatusCode.NOT_FOUND, f"unknown service {request.service!r}")
            return models.HealthCheckResponse(status=status)

        @service.unary_stream("Watch")
        async def watch(
            request: models.HealthCheckRequest,
        ) -> AsyncIterator[models.HealthCheckResponse]:
            queue: asyncio.Queue[ServingStatus] = asyncio.Queue()
            watchers = self._watchers.setdefault(request.service, set())
            watchers.add(queue)
            try:
                last = self._statuses.get(request.service, ServingStatus.SERVICE_UNKNOWN)
                yield models.HealthCheckResponse(status=last)
                while True:
                    status = await queue.get()
                    if status != last:
                        last = status
                        yield models.HealthCheckResponse(status=status)
            finally:
                watchers.discard(queue)

        @service.unary("List")
        async def list_statuses(request: models.HealthListRequest) -> models.HealthListResponse:
            statuses = {
                name: models.HealthCheckResponse(status=status)
                for name, status in self._statuses.items()
            }
            return models.HealthListResponse(statuses=statuses)

        return service
