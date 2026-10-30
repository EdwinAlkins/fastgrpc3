"""Health checking, server reflection and `fastgrpc3 run --workers`."""

import asyncio
import datetime
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import protoruf
import pytest
from pydantic import BaseModel

from fastgrpc3 import Channel, GrpcApp, GrpcClient, GrpcError, GrpcService, Insecure, StatusCode
from fastgrpc3._proto.grpc.health.v1 import health_models as health
from fastgrpc3._proto.grpc.health.v1.health_service import HealthClient
from fastgrpc3._proto.grpc.reflection.v1 import reflection_models as v1
from fastgrpc3._proto.grpc.reflection.v1.reflection_service import ServerReflectionClient
from fastgrpc3._proto.grpc.reflection.v1alpha import reflection_models as v1alpha
from fastgrpc3._proto.grpc.reflection.v1alpha.reflection_service import (
    ServerReflectionClient as AlphaReflectionClient,
)
from fastgrpc3.ops.health import ServingStatus
from fastgrpc3.testing import TestClient
from fastgrpc3.types import Empty

pytestmark = pytest.mark.usefixtures("transport")


class Ping(BaseModel):
    n: int = 0


class Dated(BaseModel):
    """Its timestamp makes the file import google/protobuf/timestamp.proto."""

    at: datetime.datetime | None = None


def make_app() -> GrpcApp:
    service = GrpcService("Pinger", package="ping.v1")

    @service.unary()
    async def Ping_(req: Ping) -> Ping:
        return req

    @service.unary()
    async def When(req: Empty) -> Dated:
        return Dated()

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    app.enable_health()
    app.enable_reflection()
    return app


async def _first[T](iterator: AsyncIterator[T], count: int) -> list[T]:
    items = []
    async for item in iterator:
        items.append(item)
        if len(items) == count:
            break
    return items


async def test_health_check_list_and_watch() -> None:
    app = make_app()
    assert app.health is not None
    async with TestClient(app) as tc:
        client = tc.client(HealthClient)
        reply = await client.check(health.HealthCheckRequest(service="ping.v1.Pinger"))
        assert reply.status == ServingStatus.SERVING
        assert (await client.check(health.HealthCheckRequest())).status == ServingStatus.SERVING
        with pytest.raises(GrpcError) as raised:
            await client.check(health.HealthCheckRequest(service="nope.v1.Nope"))
        assert raised.value.code == StatusCode.NOT_FOUND
        statuses = (await client.list(health.HealthListRequest())).statuses
        assert set(statuses) == {
            "",
            "ping.v1.Pinger",
            "grpc.health.v1.Health",
            "grpc.reflection.v1.ServerReflection",
            "grpc.reflection.v1alpha.ServerReflection",
        }

        watching = asyncio.ensure_future(
            _first(client.watch(health.HealthCheckRequest(service="ping.v1.Pinger")), 3)
        )
        unknown = asyncio.ensure_future(
            _first(client.watch(health.HealthCheckRequest(service="later.v1.Later")), 2)
        )
        await asyncio.sleep(0.1)
        app.health.set_not_serving("ping.v1.Pinger")
        app.health.set_serving("ping.v1.Pinger")
        app.health.set_serving("later.v1.Later")
        seen = [r.status for r in await asyncio.wait_for(watching, 5)]
        assert seen == [ServingStatus.SERVING, ServingStatus.NOT_SERVING, ServingStatus.SERVING]
        later = [r.status for r in await asyncio.wait_for(unknown, 5)]
        assert later == [ServingStatus.SERVICE_UNKNOWN, ServingStatus.SERVING]


async def test_health_reports_not_serving_while_stopping() -> None:
    app = make_app()
    async with TestClient(app) as tc:
        watch = tc.client(HealthClient).watch(health.HealthCheckRequest())
        assert (await anext(watch)).status == ServingStatus.SERVING
        stopping = asyncio.ensure_future(app.stop(grace=1.0))
        assert (await anext(watch)).status == ServingStatus.NOT_SERVING
        await stopping


def _descriptor_set(files: list[bytes]) -> bytes:
    """`FileDescriptorSet` bytes of `FileDescriptorProto`s (field 1, repeated)."""
    out = bytearray()
    for file in files:
        out += b"\x0a" + _varint(len(file)) + file
    return bytes(out)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


async def test_reflection_serves_the_compiled_files() -> None:
    app = make_app()
    async with TestClient(app) as tc:
        requests = [
            v1.ServerReflectionRequest(host="h", list_services=""),
            v1.ServerReflectionRequest(file_containing_symbol="ping.v1.Pinger.When"),
            v1.ServerReflectionRequest(file_by_filename="ping/v1/ping.proto"),
            v1.ServerReflectionRequest(file_containing_symbol="nope.v1.Nope"),
            v1.ServerReflectionRequest(all_extension_numbers_of_type="ping.v1.Ping"),
            v1.ServerReflectionRequest(
                file_containing_extension=v1.ExtensionRequest(containing_type="ping.v1.Ping")
            ),
        ]
        replies = [
            r async for r in tc.client(ServerReflectionClient).server_reflection_info(requests)
        ]
    assert len(replies) == 6
    listed, by_symbol, by_name, missing, extensions, extension = replies
    assert listed.valid_host == "h"
    assert listed.original_request == requests[0]
    assert listed.list_services_response is not None
    assert [s.name for s in listed.list_services_response.service] == [
        "grpc.health.v1.Health",
        "grpc.reflection.v1.ServerReflection",
        "grpc.reflection.v1alpha.ServerReflection",
        "ping.v1.Pinger",
    ]
    for reply in (by_symbol, by_name):
        assert reply.file_descriptor_response is not None
        files = reply.file_descriptor_response.file_descriptor_proto
        view = protoruf.Schema(_descriptor_set(files))
        # The file comes first, then its dependencies; together they are complete.
        assert next(iter(view.files)) == "ping/v1/ping.proto"
        assert set(view.files) == {
            "ping/v1/ping.proto",
            "google/protobuf/timestamp.proto",
            "google/protobuf/empty.proto",
        }
        assert view.service("ping.v1.Pinger").method("When").input_type == "google.protobuf.Empty"
    assert missing.error_response == v1.ErrorResponse(
        error_code=5, error_message="nope.v1.Nope not found"
    )
    assert extensions.all_extension_numbers_response == v1.ExtensionNumberResponse(
        base_type_name="ping.v1.Ping"
    )
    assert extension.error_response is not None
    assert extension.error_response.error_code == 5


async def test_reflection_v1alpha() -> None:
    async with TestClient(make_app()) as tc:
        replies = [
            r
            async for r in tc.client(AlphaReflectionClient).server_reflection_info(
                [v1alpha.ServerReflectionRequest(list_services="")]
            )
        ]
    assert replies[0].list_services_response is not None
    assert len(replies[0].list_services_response.service) == 4


WORKER_APP = """
import os

from pydantic import BaseModel
from fastgrpc3 import GrpcApp, GrpcService, Insecure


class Who(BaseModel):
    pid: int = 0


service = GrpcService("Whoami", package="who.v1")


@service.unary()
async def Ask(req: Who) -> Who:
    return Who(pid=os.getpid())


app = GrpcApp(tls=Insecure())
app.add_service(service)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.mark.skipif(sys.platform != "linux", reason="SO_REUSEPORT load balancing is Linux's")
async def test_run_with_workers_shares_one_port(tmp_path: Path) -> None:
    class Who(BaseModel):
        pid: int = 0

    class Asker(GrpcClient):
        package = "who.v1"
        service = "Whoami"
        ask = GrpcClient.unary("Ask", Who, Who)

    (tmp_path / "whoapp.py").write_text(textwrap.dedent(WORKER_APP), encoding="utf-8")
    port = _free_port()
    command = [sys.executable, "-m", "fastgrpc3", "run", "whoapp:app", "--workers", "2"]
    command += ["--address", f"127.0.0.1:{port}", "--grace", "0.5"]
    parent = subprocess.Popen(command, cwd=tmp_path)
    try:
        pids: set[int] = set()
        for _ in range(100):  # until both workers answered, 10 s at most
            channel = Channel(f"127.0.0.1:{port}", tls=Insecure(), default_timeout=1.0)
            async with channel:
                try:
                    pids.add((await Asker(channel).ask(Who())).pid)
                except GrpcError:
                    await asyncio.sleep(0.1)
            if len(pids) == 2:
                break
        assert len(pids) == 2
        assert parent.pid not in pids
    finally:
        parent.send_signal(signal.SIGTERM)
        assert parent.wait(timeout=10) == 0
    with pytest.raises(ProcessLookupError):
        for pid in pids:
            os.kill(pid, 0)


def _tool(name: str) -> str:
    path = shutil.which(name) or shutil.which(name, path=str(Path.home() / "go" / "bin"))
    if path is None:
        pytest.skip(f"{name} is not installed")
    return path


async def _run_tool(*command: str) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run, command, capture_output=True, text=True, timeout=30, check=False
    )


async def test_grpcurl_and_grpc_health_probe() -> None:
    """Criterion R7, with the reference tools (skipped when they are not installed)."""
    grpcurl, probe = _tool("grpcurl"), _tool("grpc-health-probe")
    app = make_app()
    async with TestClient(app):
        address = f"127.0.0.1:{app.bound_port}"
        listed = await _run_tool(grpcurl, "-plaintext", address, "list")
        described = await _run_tool(grpcurl, "-plaintext", address, "describe", "ping.v1.Pinger")
        called = await _run_tool(
            grpcurl, "-plaintext", "-d", '{"n": 3}', address, "ping.v1.Pinger/Ping_"
        )
        healthy = await _run_tool(probe, f"-addr={address}", "-service=ping.v1.Pinger")
        unknown = await _run_tool(probe, f"-addr={address}", "-service=nope.v1.Nope")
    assert listed.stdout.split() == [
        "grpc.health.v1.Health",
        "grpc.reflection.v1.ServerReflection",
        "grpc.reflection.v1alpha.ServerReflection",
        "ping.v1.Pinger",
    ]
    assert "rpc When ( .google.protobuf.Empty ) returns ( .ping.v1.Dated );" in described.stdout
    assert '"n": "3"' in called.stdout  # int64 is a JSON string
    # grpc-health-probe reports on stderr.
    assert (healthy.returncode, healthy.stderr.strip()) == (0, "status: SERVING")
    assert unknown.returncode != 0
