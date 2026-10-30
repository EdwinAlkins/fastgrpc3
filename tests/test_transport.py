"""Choosing the transport: `GrpcApp(transport=...)`, `FASTGRPC_TRANSPORT`, refusals."""

import pytest

from fastgrpc3 import GrpcApp, Insecure


async def test_unknown_transports_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown transport 'h2'"):
        GrpcApp(tls=Insecure(), transport="h2")  # type: ignore[arg-type]
    monkeypatch.setenv("FASTGRPC_TRANSPORT", "h2")
    app = GrpcApp(tls=Insecure())
    app.enable_health()  # an app needs a service to start
    with pytest.raises(ValueError, match="unknown transport 'h2'"):
        await app.start()


async def test_tonic_refuses_the_settings_it_does_not_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastgrpc3_transport")
    monkeypatch.delenv("FASTGRPC_TRANSPORT", raising=False)
    app = GrpcApp(tls=Insecure(), transport="tonic", options=[("grpc.x", 1)], max_recv_bytes=1024)
    app.enable_health()
    with pytest.raises(ValueError, match="not support yet: options, max_recv_bytes"):
        await app.start()
    assert app.bound_port is None


async def test_the_environment_variable_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTGRPC_TRANSPORT", "grpc_aio")
    app = GrpcApp(tls=Insecure(), address="127.0.0.1:0", transport="tonic", options=[("x", 1)])
    app.enable_health()
    await app.start()  # grpc_aio takes any option
    await app.stop(None)
