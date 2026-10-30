"""Shared fixtures."""

import pytest


@pytest.fixture(params=["grpc_aio", "tonic"])
def transport(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test on each transport: `pytestmark = pytest.mark.usefixtures("transport")`."""
    if request.param == "tonic":
        pytest.importorskip("fastgrpc3_transport")
    monkeypatch.setenv("FASTGRPC_TRANSPORT", request.param)
    return str(request.param)
