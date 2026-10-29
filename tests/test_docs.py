"""Interactive docs: the OpenAPI document, Swagger UI and the JSON -> gRPC gateway."""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from fastgrpc3 import (
    TLS,
    Context,
    ErrorInfo,
    GrpcApp,
    GrpcError,
    GrpcService,
    Insecure,
    ProtoEnum,
    StatusCode,
)
from fastgrpc3.cli import main
from fastgrpc3.ops.openapi import openapi
from fastgrpc3.runtime.workers import serve
from fastgrpc3.testing import TestClient


class Color(ProtoEnum):
    """A color."""

    UNSPECIFIED = 0
    RED = 1


class Item(BaseModel):
    """An item."""

    name: str = Field(min_length=1, description="Its name.")
    count: int = 0
    color: Color = Color.UNSPECIFIED


class Total(BaseModel):
    count: int = 0


def make_service() -> GrpcService:
    service = GrpcService("Shop", package="shop.v1", doc="The shop.")

    @service.unary()
    async def Get(item: Item, ctx: Context) -> Item:
        """Get an item back."""
        if item.name == "missing":
            raise GrpcError(StatusCode.NOT_FOUND, "no such item", [ErrorInfo(reason="GONE")])
        user = str(ctx.metadata.get("x-user", "nobody"))
        return Item(name=f"{item.name} for {user}", count=item.count, color=item.color)

    @service.unary_stream()
    async def Repeat(item: Item) -> AsyncIterator[Item]:
        for i in range(item.count):
            yield Item(name=f"{item.name} {i}")
        if item.name == "fail":
            raise GrpcError(StatusCode.FAILED_PRECONDITION, "stopped")

    @service.stream_unary()
    async def Count(items: AsyncIterator[Item]) -> Total:
        return Total(count=sum([item.count async for item in items]))

    @service.stream_stream()
    async def Echo(items: AsyncIterator[Item]) -> AsyncIterator[Item]:
        async for item in items:
            yield item

    return service


def make_app(**docs: Any) -> GrpcApp:
    app = GrpcApp("shop", tls=Insecure())
    app.add_service(make_service())
    app.enable_reflection()
    app.enable_docs(port=0, **docs)
    return app


async def http(url: str, body: bytes | None = None, **headers: str) -> tuple[int, bytes]:
    def call() -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    return await asyncio.to_thread(call)


def test_openapi_document() -> None:
    app = make_app()
    document = openapi(app.services, title="shop", metadata=["x-user"])
    paths = document["paths"]
    assert list(paths) == [f"/shop.v1.Shop/{name}" for name in ("Get", "Repeat", "Count", "Echo")]
    get = paths["/shop.v1.Shop/Get"]["post"]
    assert get["description"] == "Get an item back."
    assert get["parameters"][0]["name"] == "x-user"
    assert get["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert get["responses"]["default"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Status"
    }
    assert list(paths["/shop.v1.Shop/Repeat"]["post"]["responses"]["200"]["content"]) == [
        "application/x-ndjson"
    ]
    assert list(paths["/shop.v1.Shop/Count"]["post"]["requestBody"]["content"]) == [
        "application/x-ndjson"
    ]
    schemas = document["components"]["schemas"]
    assert schemas["Item"]["properties"]["name"]["minLength"] == 1
    assert schemas["Color"]["x-enum-varnames"] == ["UNSPECIFIED", "RED"]
    assert "`1` RED" in schemas["Color"]["description"]
    assert document["tags"] == [{"name": "shop.v1.Shop", "description": "The shop."}]


async def test_gateway_calls_the_service() -> None:
    app = make_app(gateway=True, metadata=["X-User"])
    async with TestClient(app):
        assert app.docs is not None and app.docs.port
        base = f"http://127.0.0.1:{app.docs.port}"
        shop = f"{base}/shop.v1.Shop"

        status, page = await http(f"{base}/docs")
        assert status == 200 and b'"supportedSubmitMethods": ["post"]' in page
        status, document = await http(f"{base}/openapi.json")
        assert status == 200 and "/shop.v1.Shop/Get" in json.loads(document)["paths"]

        request = b'{"name": "pen", "color": 1}'
        status, body = await http(f"{shop}/Get", request, **{"x-user": "ann"})
        assert (status, json.loads(body)) == (200, {"name": "pen for ann", "count": 0, "color": 1})

        status, body = await http(f"{shop}/Get", b'{"name": "missing"}')
        error = json.loads(body)
        assert (status, error["status"], error["message"]) == (404, "NOT_FOUND", "no such item")
        assert error["details"][0]["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
        assert error["details"][0]["reason"] == "GONE"

        status, body = await http(f"{shop}/Get", b'{"name": ""}')
        error = json.loads(body)
        assert (status, error["status"]) == (400, "INVALID_ARGUMENT")
        assert error["details"][0]["field_violations"][0]["field"] == "name"

        status, body = await http(f"{shop}/Repeat", b'{"name": "a", "count": 2}')
        lines = [json.loads(line) for line in body.splitlines()]
        assert status == 200 and [line["name"] for line in lines] == ["a 0", "a 1"]

        status, body = await http(f"{shop}/Repeat", b'{"name": "fail", "count": 1}')
        lines = [json.loads(line) for line in body.splitlines()]
        assert status == 200 and lines[0]["name"] == "fail 0"
        assert lines[1]["error"]["status"] == "FAILED_PRECONDITION"

        status, body = await http(f"{shop}/Repeat", b'{"name": "fail", "count": 0}')
        assert (status, json.loads(body)["status"]) == (400, "FAILED_PRECONDITION")

        stream = b'{"name": "a", "count": 2}\n\n{"name": "b", "count": 3}\n'
        status, body = await http(f"{shop}/Count", stream)
        assert (status, json.loads(body)) == (200, {"count": 5})
        status, body = await http(f"{shop}/Echo", stream)
        assert [json.loads(line)["name"] for line in body.splitlines()] == ["a", "b"]

        status, body = await http(f"{base}/grpc.reflection.v1.ServerReflection/X", b"{}")
        assert (status, json.loads(body)["status"]) == (404, "NOT_FOUND")


async def test_docs_without_gateway_only_document() -> None:
    app = make_app()
    async with TestClient(app):
        assert app.docs is not None
        base = f"http://127.0.0.1:{app.docs.port}"
        status, page = await http(f"{base}/docs")
        assert status == 200 and b'"supportedSubmitMethods": []' in page
        status, body = await http(f"{base}/shop.v1.Shop/Get", b'{"name": "pen"}')
        assert (status, json.loads(body)["status"]) == (501, "UNIMPLEMENTED")
    assert app.docs.port is not None  # the port of the last run stays readable


def test_docs_settings_are_checked() -> None:
    app = GrpcApp(tls=TLS(cert="cert.pem", key="key.pem"))
    with pytest.raises(ValueError, match="gateway_tls"):
        app.enable_docs(gateway=True)
    app.enable_docs()  # documentation only: no channel needed
    with pytest.raises(ValueError, match="single worker"):
        serve(lambda _: app, "app:app", "127.0.0.1:50999", 2, 1.0)


def test_cli_writes_the_openapi_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = tmp_path / "docs_shop_app.py"
    module.write_text(
        "from fastgrpc3 import GrpcApp, Insecure\n"
        "from tests.test_docs import make_service\n"
        "app = GrpcApp('shop', tls=Insecure())\n"
        "app.add_service(make_service())\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    assert main(["openapi", "docs_shop_app:app", "-o", "openapi.json"]) == 0
    document = json.loads((tmp_path / "openapi.json").read_text())
    assert document["info"]["title"] == "shop"
    assert "/shop.v1.Shop/Echo" in document["paths"]
