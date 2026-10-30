"""Choosing the transport: `GrpcApp(transport=...)`, `FASTGRPC_TRANSPORT`, `run --transport`,
availability, version and capability checks."""

import asyncio
import importlib.util
import os
import subprocess
import sys
from typing import Any

import grpc.aio
import pytest

from fastgrpc3 import GrpcApp, Insecure, Transport, available_transports, cli
from fastgrpc3._proto.grpc.health.v1.health_models import HealthCheckRequest
from fastgrpc3._proto.grpc.health.v1.health_service import HealthClient
from fastgrpc3.ops.health import ServingStatus
from fastgrpc3.testing import TestClient


def app_with(**options: Any) -> GrpcApp:
    app = GrpcApp(tls=Insecure(), address="127.0.0.1:0", **options)
    app.enable_health()  # an app needs a service to start
    return app


@pytest.fixture(autouse=True)
def no_transport_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FASTGRPC_TRANSPORT", raising=False)


async def test_unknown_transports_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown transport 'h2'"):
        GrpcApp(tls=Insecure(), transport="h2")
    monkeypatch.setenv("FASTGRPC_TRANSPORT", "h2")
    with pytest.raises(ValueError, match="unknown transport 'h2'"):
        await app_with().start()


def test_transports_are_an_enum_or_their_name() -> None:
    assert GrpcApp(tls=Insecure()).transport is Transport("grpc_aio")
    assert GrpcApp(tls=Insecure(), transport="tonic").transport is Transport.TONIC


def test_the_command_line_wins_over_the_variable_which_wins_over_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GrpcApp(tls=Insecure(), transport=Transport.TONIC)
    selected = [app.selected_transport]
    monkeypatch.setenv("FASTGRPC_TRANSPORT", "grpc_aio")
    selected.append(app.selected_transport)
    assert selected == [Transport.TONIC, Transport.GRPC_AIO]

    seen = []
    monkeypatch.setattr(cli, "serve", lambda *_: seen.append(os.environ["FASTGRPC_TRANSPORT"]))
    cli.main(["run", "app:app", "--transport", "tonic"])
    assert seen == ["tonic"]


def test_importing_fastgrpc3_does_not_load_the_extension() -> None:
    code = "import sys, fastgrpc3; fastgrpc3.available_transports(); print(sorted(sys.modules))"
    modules = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    ).stdout
    assert "fastgrpc3_transport" not in modules


async def test_a_missing_transport_is_reported_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "fastgrpc3_transport" else real(name, *a),
    )
    assert available_transports() == [Transport.GRPC_AIO]
    with pytest.raises(RuntimeError, match=r'pip install "fastgrpc3\[tonic\]"'):
        await app_with(transport="tonic").start()


async def test_an_incompatible_transport_is_reported_at_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = pytest.importorskip("fastgrpc3_transport")
    assert available_transports() == [Transport.GRPC_AIO, Transport.TONIC]
    monkeypatch.setattr(native, "TRANSPORT_API_VERSION", 3)
    monkeypatch.delitem(sys.modules, "fastgrpc3.transport.tonic", raising=False)
    with pytest.raises(RuntimeError, match="Required transport API: 2\nDetected transport API: 3"):
        await app_with(transport="tonic").start()


async def test_tonic_refuses_what_it_cannot_do() -> None:
    pytest.importorskip("fastgrpc3_transport")
    app = app_with(transport="tonic", options=[("grpc.x", 1)])
    assert not app.transport_capabilities.raw_grpc_options
    with pytest.raises(ValueError, match=r"does not support the grpc option 'grpc\.x'"):
        await app.start()
    assert app.bound_port is None

    class Native(grpc.aio.ServerInterceptor):
        async def intercept_service(self, continuation: Any, details: Any) -> Any:
            return await continuation(details)

    app = app_with(transport="tonic", server_interceptors=[Native()])
    assert not app.transport_capabilities.native_grpc_aio_interceptors
    with pytest.raises(ValueError, match="Use fastgrpc3 middleware or select grpc_aio"):
        await app.start()


async def test_grpc_aio_takes_any_option() -> None:
    app = app_with(options=[("grpc.so_reuseport", 0)])
    assert app.transport_capabilities.raw_grpc_options
    await app.start()
    await app.stop(None)


async def test_tonic_serves_where_the_loop_cannot_watch_a_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows' Proactor loop has no `add_reader`: a task reads the wake-ups instead."""
    pytest.importorskip("fastgrpc3_transport")
    from fastgrpc3.transport.tonic import TonicTransport

    loop = asyncio.get_running_loop()
    add_reader = loop.add_reader

    def proactor_like(fd: Any, callback: Any, *args: Any) -> None:
        if isinstance(getattr(callback, "__self__", None), TonicTransport):
            raise NotImplementedError
        add_reader(fd, callback, *args)  # grpc's client still watches its own socket

    monkeypatch.setattr(loop, "add_reader", proactor_like)
    app = app_with(transport="tonic")
    async with TestClient(app) as tc:
        assert isinstance(app._server, TonicTransport) and app._server._waker_task
        reply = await tc.client(HealthClient).check(HealthCheckRequest())
    assert reply.status == ServingStatus.SERVING
