"""TLS and mutual TLS: the client's identity, refused clients."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from fastgrpc3 import MTLS, TLS, Context, GrpcApp, GrpcClient, GrpcError, GrpcService, StatusCode
from fastgrpc3.api.client import Channel
from fastgrpc3.api.security import Security

pytestmark = pytest.mark.usefixtures("transport")


class Text(BaseModel):
    text: str = ""


class WhoClient(GrpcClient):
    package = "who.v1"
    service = "Who"
    ask = GrpcClient.unary("Ask", Text, Text)


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A CA, a server certificate for localhost, a client `alice` and an unknown client."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    d = tmp_path_factory.mktemp("pki")

    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=d)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", "ca.key",
        "-out", "ca.pem", "-subj", "/CN=test-ca")  # fmt: skip
    for name, subject, names in (
        ("server", "/CN=localhost", "DNS:localhost,IP:127.0.0.1"),
        ("alice", "/CN=alice", "DNS:alice.example,URI:spiffe://test/alice"),
    ):
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", "x.csr",
            "-subj", subject)  # fmt: skip
        (d / "ext").write_text(f"subjectAltName={names}\n")
        run(
            *("x509", "-req", "-in", "x.csr", "-CA", "ca.pem", "-CAkey", "ca.key"),
            *("-CAcreateserial", "-days", "1", "-out", f"{name}.pem", "-extfile", "ext"),
        )
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", "eve.key",
        "-out", "eve.pem", "-subj", "/CN=eve")  # fmt: skip
    return d


def make_app(tls: Security) -> GrpcApp:
    service = GrpcService("Who", package="who.v1")

    @service.unary()
    async def Ask(req: Text, ctx: Context) -> Text:
        context = ctx.auth_context()
        seen = {
            "identities": [i.decode() for i in ctx.peer_identities()],
            "context": {k: [v.decode() for v in context[k]] for k in KEYS if k in context},
        }
        return Text(text=json.dumps(seen))

    app = GrpcApp(tls=tls, address="localhost:0")
    app.add_service(service)
    return app


KEYS = (
    "transport_security_type",
    "x509_common_name",
    "x509_subject_alternative_name",
    "peer_dns",
    "peer_spiffe_id",
)


async def ask(app: GrpcApp, tls: TLS | MTLS) -> dict[str, object]:
    async with Channel(f"localhost:{app.bound_port}", tls=tls, default_timeout=5) as channel:
        result: dict[str, object] = json.loads((await WhoClient(channel).ask(Text())).text)
        return result


async def test_mutual_tls_gives_the_client_identity(pki: Path) -> None:
    server = MTLS(cert=pki / "server.pem", key=pki / "server.key", ca=pki / "ca.pem")
    async with make_app(server).serving() as app:
        seen = await ask(
            app, MTLS(cert=pki / "alice.pem", key=pki / "alice.key", ca=pki / "ca.pem")
        )
    names = ["alice.example", "spiffe://test/alice"]
    assert seen == {
        "identities": names,
        "context": {
            "transport_security_type": ["ssl"],
            "x509_common_name": ["alice"],
            "x509_subject_alternative_name": names,
            "peer_dns": ["alice.example"],
            "peer_spiffe_id": ["spiffe://test/alice"],
        },
    }


async def test_tls_alone_has_no_client_identity(pki: Path) -> None:
    async with make_app(TLS(cert=pki / "server.pem", key=pki / "server.key")).serving() as app:
        seen = await ask(app, TLS(ca=pki / "ca.pem"))
    assert seen == {"identities": [], "context": {"transport_security_type": ["ssl"]}}


async def test_mutual_tls_refuses_unknown_or_missing_client_certificates(pki: Path) -> None:
    server = MTLS(cert=pki / "server.pem", key=pki / "server.key", ca=pki / "ca.pem")
    async with make_app(server).serving() as app:
        for client in (
            MTLS(cert=pki / "eve.pem", key=pki / "eve.key", ca=pki / "ca.pem"),
            TLS(ca=pki / "ca.pem"),
        ):
            with pytest.raises(GrpcError) as raised:
                await ask(app, client)
            assert raised.value.code == StatusCode.UNAVAILABLE
