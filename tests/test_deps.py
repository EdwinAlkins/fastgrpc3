"""Dependency injection: sub-dependencies, cache, cleanup, overrides, signatures."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from fastgrpc3 import (
    Context,
    Depends,
    GrpcApp,
    GrpcClient,
    GrpcError,
    GrpcService,
    Insecure,
    StatusCode,
    blocking,
    non_blocking,
)
from fastgrpc3.api.deps import SLOW_PROVIDER_SECONDS, plan_dependencies
from fastgrpc3.testing import TestClient


class Req(BaseModel):
    n: int = 0


class Rep(BaseModel):
    text: str = ""


class DepsClient(GrpcClient):
    package = "deps.v1"
    service = "Deps"
    get = GrpcClient.unary("Get", Req, Rep)
    stream = GrpcClient.unary_stream("Stream", Req, Rep)


async def test_sub_dependencies_cache_cleanup_and_context() -> None:
    events: list[str] = []
    calls = {"settings": 0}

    def settings() -> dict[str, str]:
        calls["settings"] += 1
        return {"db": "memory"}

    async def user(ctx: Context) -> str:
        return str(ctx.metadata.get("x-user", "anon"))

    def connection(config: dict[str, str] = Depends(settings)) -> Iterator[str]:
        events.append("open")
        yield f"conn:{config['db']}"
        events.append("close")

    async def repository(
        conn: Annotated[str, Depends(connection)], config: dict[str, str] = Depends(settings)
    ) -> AsyncIterator[str]:
        events.append("repo up")
        yield f"repo({conn})"
        events.append("repo down")

    service = GrpcService("Deps", package="deps.v1")

    @service.unary()
    async def Get(
        req: Req,
        repo: str = Depends(repository),
        who: Annotated[str, Depends(user)] = "",
        again: dict[str, str] = Depends(settings),
    ) -> Rep:
        events.append("handler")
        return Rep(text=f"{repo} for {who}")

    @service.unary_stream()
    async def Stream(req: Req, repo: str = Depends(repository)) -> AsyncIterator[Rep]:
        for i in range(req.n):
            events.append(f"item {i}")
            yield Rep(text=repo)

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    async with TestClient(app) as tc:
        deps = tc.client(DepsClient)
        reply = await deps.get(Req(), metadata={"x-user": "alice"})
        assert reply.text == "repo(conn:memory) for alice"
        assert calls["settings"] == 1  # once per call, shared by three users
        assert events == ["open", "repo up", "handler", "repo down", "close"]

        events.clear()
        assert [r.text async for r in deps.stream(Req(n=2))] == ["repo(conn:memory)"] * 2
        # Cleanup runs once the stream is over.
        assert events == ["open", "repo up", "item 0", "item 1", "repo down", "close"]


async def test_overrides_replace_providers_when_the_server_starts() -> None:
    def real() -> str:
        return "real"

    service = GrpcService("Deps", package="deps.v1")

    @service.unary()
    async def Get(req: Req, value: str = Depends(real)) -> Rep:
        return Rep(text=value)

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    app.dependency_overrides[real] = lambda: "fake"
    async with TestClient(app) as tc:
        assert (await tc.client(DepsClient).get(Req())).text == "fake"


def test_use_cache_false_calls_again() -> None:
    def counter() -> int:
        return 1

    def first(value: int = Depends(counter, use_cache=False)) -> int:
        return value

    plan = plan_dependencies(
        {"a": Depends(counter, use_cache=False), "b": Depends(first)}, overrides={}
    )
    assert [node.provider for node in plan.nodes] == [counter, counter, first]


def test_dependency_cycles_are_refused() -> None:
    def a(value: int = Depends(lambda: 0)) -> int:
        return value

    def b(value: int = Depends(a)) -> int:
        return value

    with pytest.raises(TypeError, match="dependency cycle"):
        plan_dependencies({"x": Depends(b)}, overrides={a: b})


async def test_a_dependency_cannot_swallow_the_handler_error() -> None:
    """A generator that catches the error without raising it again still runs its
    cleanup, outer dependencies see the error too, and the call fails."""
    events: list[str] = []

    def transaction() -> Iterator[str]:
        try:
            yield "tx"
        except Exception as e:
            events.append(f"commit skipped: {e}")
        else:
            events.append("commit")

    async def session(tx: str = Depends(transaction)) -> AsyncIterator[str]:
        try:
            yield "session"
        except Exception as e:
            events.append(f"rollback: {e}")  # swallowed

    service = GrpcService("Deps", package="deps.v1")

    @service.unary()
    async def Get(req: Req, s: str = Depends(session)) -> Rep:
        raise RuntimeError("boom")

    @service.unary_stream()
    async def Stream(req: Req, s: str = Depends(session)) -> AsyncIterator[Rep]:
        yield Rep(text="first")
        raise RuntimeError("late")

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    async with TestClient(app) as tc:
        deps = tc.client(DepsClient)
        with pytest.raises(GrpcError) as unary:
            await deps.get(Req())
        assert (unary.value.code, unary.value.message) == (
            StatusCode.INTERNAL,
            "internal server error",
        )
        assert events == ["rollback: boom", "commit skipped: boom"]

        events.clear()
        received = []
        with pytest.raises(GrpcError) as stream:
            async for reply in deps.stream(Req()):
                received.append(reply.text)
        assert received == ["first"]
        assert stream.value.code == StatusCode.INTERNAL
        assert events == ["rollback: late", "commit skipped: late"]


async def test_dependencies_are_resolved_inside_the_middlewares() -> None:
    """A middleware that refuses a call opens no dependency."""
    events: list[str] = []

    async def resource() -> AsyncIterator[str]:
        events.append("open")
        yield "r"
        events.append("close")

    service = GrpcService("Deps", package="deps.v1")

    @service.unary()
    async def Get(req: Req, r: str = Depends(resource)) -> Rep:
        events.append("handler")
        return Rep(text=r)

    app = GrpcApp(tls=Insecure())
    app.add_service(service)

    @app.middleware
    async def guard(ctx: Context, call_next: Any) -> Any:
        if ctx.metadata.get("x-user") is None:
            raise GrpcError(StatusCode.PERMISSION_DENIED, "who are you")
        events.append("allowed")
        return await call_next()

    async with TestClient(app) as tc:
        deps = tc.client(DepsClient)
        with pytest.raises(GrpcError, match="who are you"):
            await deps.get(Req())
        assert events == []
        assert (await deps.get(Req(), metadata={"x-user": "alice"})).text == "r"
        assert events == ["allowed", "open", "handler", "close"]


async def test_blocking_providers_do_not_block_the_loop() -> None:
    events: list[str] = []

    @blocking
    def slow() -> int:
        time.sleep(0.1)  # blocking I/O
        return 1

    @blocking
    def session() -> Iterator[str]:
        time.sleep(0.05)
        try:
            yield "s"
        except Exception as e:
            events.append(f"rollback {e}")
            raise
        finally:
            events.append("closed")

    service = GrpcService("Deps", package="deps.v1")

    @service.unary()
    async def Get(
        req: Req,
        n: int = Depends(slow),
        s: str = Depends(session),
    ) -> Rep:
        if req.n:
            raise RuntimeError("boom")
        return Rep(text=f"{s}{n}")

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    async with TestClient(app) as tc:
        deps = tc.client(DepsClient)
        started = time.perf_counter()
        replies = await asyncio.gather(*(deps.get(Req()) for _ in range(4)))
        assert time.perf_counter() - started < 0.4  # 0.6 s if run one after the other
        assert [r.text for r in replies] == ["s1"] * 4
        assert events == ["closed"] * 4

        events.clear()
        with pytest.raises(GrpcError):
            await deps.get(Req(n=1))
        assert events == ["rollback boom", "closed"]


async def test_a_slow_provider_on_the_loop_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def slow() -> int:
        time.sleep(SLOW_PROVIDER_SECONDS * 2)
        return 1

    service = GrpcService("Deps", package="deps.v1")

    @service.unary()
    async def Get(req: Req, n: int = Depends(slow)) -> Rep:
        return Rep(text=str(n))

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    async with TestClient(app) as tc:
        with caplog.at_level(logging.WARNING, logger="fastgrpc3"):
            await tc.client(DepsClient).get(Req())
            await tc.client(DepsClient).get(Req())
    warnings = [r.getMessage() for r in caplog.records if "blocked the event loop" in r.message]
    assert len(warnings) == 1
    assert "slow" in warnings[0] and "@blocking" in warnings[0]


def test_where_def_providers_run() -> None:
    """A mark on the provider wins; unmarked `def` providers follow the app setting."""

    def plain() -> int:
        return 1

    @blocking
    def io() -> int:
        return 2

    @non_blocking
    def quick() -> int:
        return 3

    async def coroutine() -> int:
        return 4

    providers = (plain, io, quick, coroutine)
    dependencies = {f"d{i}": Depends(provider) for i, provider in enumerate(providers)}
    on_loop = plan_dependencies(dependencies, overrides={})
    assert [node.threadpool for node in on_loop.nodes] == [False, True, False, False]
    in_threads = plan_dependencies(dependencies, overrides={}, threadpool=True)
    assert [node.threadpool for node in in_threads.nodes] == [True, True, False, False]
    with pytest.raises(TypeError, match="only `def` providers run in threads"):
        blocking(coroutine)


async def test_the_app_can_run_every_def_provider_in_a_thread() -> None:
    def slow() -> int:
        time.sleep(0.1)
        return 1

    service = GrpcService("Deps", package="deps.v1")

    @service.unary()
    async def Get(req: Req, n: int = Depends(slow)) -> Rep:
        return Rep(text=str(n))

    app = GrpcApp(tls=Insecure(), sync_providers="threadpool")
    app.add_service(service)
    async with TestClient(app) as tc:
        started = time.perf_counter()
        await asyncio.gather(*(tc.client(DepsClient).get(Req()) for _ in range(4)))
        assert time.perf_counter() - started < 0.3
    with pytest.raises(ValueError, match="sync_providers"):
        GrpcApp(tls=Insecure(), sync_providers="threads")  # type: ignore[arg-type]


def test_provider_parameters_must_be_filled() -> None:
    def needs(thing: str) -> str:
        return thing

    with pytest.raises(TypeError, match="parameter 'thing' is neither"):
        plan_dependencies({"x": Depends(needs)}, overrides={})
