"""Client: transport rules, deadline propagation, invalid responses, TLS with a token."""

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from fastgrpc3 import (
    TLS,
    BearerToken,
    Channel,
    Context,
    GrpcApp,
    GrpcClient,
    GrpcError,
    GrpcService,
    Insecure,
    RetryPolicy,
    StatusCode,
)
from fastgrpc3.testing import TestClient

pytestmark = pytest.mark.usefixtures("transport")


class Req(BaseModel):
    n: int = 0


class Rep(BaseModel):
    text: str = ""
    seconds: float = 0.0


class EchoClient(GrpcClient):
    package = "echo.v1"
    service = "Echo"
    get = GrpcClient.unary("Get", Req, Rep)


def echo_app(**settings: object) -> GrpcApp:
    service = GrpcService("Echo", package="echo.v1")

    @service.unary()
    async def Get(req: Req, ctx: Context) -> Rep:
        left = ctx.time_remaining()
        token = ctx.metadata.get("authorization", "")
        return Rep(text=str(token), seconds=-1.0 if left is None else left)

    app = GrpcApp(**settings)  # type: ignore[arg-type]
    app.add_service(service)
    return app


def test_channels_refuse_credentials_in_clear() -> None:
    with pytest.raises(ValueError, match="choose the transport explicitly"):
        Channel("localhost:1")
    with pytest.raises(ValueError, match="credentials are refused on an insecure channel"):
        Channel("localhost:1", tls=Insecure(), auth=BearerToken("t"))
    with pytest.raises(ValueError, match="no line break"):
        BearerToken("a\nb")
    assert "secret" not in repr(BearerToken("secret"))


async def test_authorization_metadata_is_refused_on_an_insecure_channel() -> None:
    async with TestClient(echo_app(tls=Insecure())) as tc:
        with pytest.raises(ValueError, match="credentials are refused"):
            await tc.client(EchoClient).get(Req(), metadata={"authorization": "Bearer x"})


async def test_a_call_made_by_a_handler_inherits_the_deadline() -> None:
    backend = echo_app(tls=Insecure())
    async with TestClient(backend) as backend_tc:
        front = GrpcService("Front", package="echo.v1")

        @front.unary()
        async def Relay(req: Req) -> Rep:
            # No timeout given: the backend call gets what is left of ours.
            return await EchoClient(backend_tc.channel).get(req, timeout=None)

        class FrontClient(GrpcClient):
            package = "echo.v1"
            service = "Front"
            relay = GrpcClient.unary("Relay", Req, Rep)

        app = GrpcApp(tls=Insecure())
        app.add_service(front)
        async with TestClient(app, default_timeout=None) as tc:
            relayed = await tc.client(FrontClient).relay(Req(), timeout=2.0)
            unbounded = await EchoClient(backend_tc.channel).get(Req(), timeout=30.0)
    # gRPC rounds the deadline it carries; without inheritance it would be the 10 s default.
    assert 0 < relayed.seconds < 2.5
    assert unbounded.seconds > 9.0


async def test_a_response_that_does_not_validate_raises() -> None:
    class StrictRep(BaseModel):
        text: str = Field(min_length=3)

    class StrictClient(GrpcClient):
        package = "echo.v1"
        service = "Echo"
        get = GrpcClient.unary("Get", Req, StrictRep)

    async with TestClient(echo_app(tls=Insecure())) as tc:
        with pytest.raises(GrpcError) as raised:
            await tc.client(StrictClient).get(Req())
    assert (raised.value.code, raised.value.message) == (
        StatusCode.INTERNAL,
        "invalid response payload",
    )


def test_retry_policies_name_their_methods() -> None:
    with pytest.raises(ValueError, match="idempotent methods"):
        RetryPolicy(methods=())
    with pytest.raises(ValueError, match="between 2 and 5"):
        RetryPolicy(methods=("echo.v1.Echo/Get",), max_attempts=9)
    policy = RetryPolicy(methods=("echo.v1.Echo/Get",))
    assert '"service": "echo.v1.Echo", "method": "Get"' in policy.service_config()


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    directory = tmp_path_factory.mktemp("tls")
    key, cert = directory / "key.pem", directory / "cert.pem"
    subprocess.run(
        [
            *("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"),
            *("-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost"),
            *("-addext", "subjectAltName=DNS:localhost"),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


async def test_tls_with_a_bearer_token(certificates: tuple[Path, Path]) -> None:
    cert, key = certificates
    app = echo_app(tls=TLS(cert=cert, key=key), address="localhost:0")
    async with app.serving():
        channel = Channel(
            f"localhost:{app.bound_port}", tls=TLS(ca=cert.read_bytes()), auth=BearerToken("t0k")
        )
        async with channel:
            reply = await EchoClient(channel).get(Req())
    assert reply.text == "Bearer t0k"
