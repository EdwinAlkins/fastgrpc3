"""Server runtime through `TestClient`: calls, errors, hooks, middlewares, lifespan."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest
from pydantic import BaseModel, Field

from fastgrpc3 import (
    BadRequest,
    Context,
    ErrorInfo,
    FieldViolation,
    GrpcApp,
    GrpcClient,
    GrpcError,
    GrpcService,
    Insecure,
    StatusCode,
    register_detail,
)
from fastgrpc3.runtime.dispatch import Invoker
from fastgrpc3.testing import TestClient
from fastgrpc3.transport.base import Pairs, RpcFailure
from fastgrpc3.types import AnyMessage, field

pytestmark = pytest.mark.usefixtures("transport")


class Req(BaseModel):
    n: int = 0
    text: str = ""


class Rep(BaseModel):
    total: int = 0
    text: str = ""


@register_detail
class Quota(BaseModel):
    """A detail of our own."""

    left: int = 0


class Unregistered(BaseModel):
    why: str = ""


def make_service() -> GrpcService:
    service = GrpcService("Calc", package="calc.v1")

    @service.unary()
    async def Double(req: Req, ctx: Context) -> Rep:
        return Rep(total=req.n * 2, text=ctx.method)

    @service.unary_stream("Count")
    async def count(req: Req) -> AsyncIterator[Rep]:
        for i in range(req.n):
            yield Rep(total=i)

    @service.stream_unary("Sum")
    async def total(requests: AsyncIterator[Req]) -> Rep:
        return Rep(total=sum([r.n async for r in requests]))

    @service.stream_stream("Echo")
    async def echo(requests: AsyncIterator[Req]) -> AsyncIterator[Rep]:
        async for r in requests:
            yield Rep(total=r.n, text=r.text)

    @service.unary("Fail")
    async def fail(req: Req) -> Rep:
        if req.text == "grpc":
            raise GrpcError(
                StatusCode.FAILED_PRECONDITION,
                "not now",
                [
                    ErrorInfo(reason="BUSY", domain="calc", metadata={"k": "v"}),
                    BadRequest(field_violations=[FieldViolation(field="n", description="odd")]),
                    Quota(left=3),
                    Unregistered(why="secret"),
                ],
            )
        if req.text == "dict":
            return {"total": 7}  # type: ignore[return-value]  # validated into Rep
        if req.text == "bad response":
            return {"total": "not a number"}  # type: ignore[return-value]
        if req.text == "custom":
            raise KeyError(req.n)
        raise RuntimeError("boom")

    return service


class CalcClient(GrpcClient):
    package = "calc.v1"
    service = "Calc"
    double = GrpcClient.unary("Double", Req, Rep)
    count = GrpcClient.unary_stream("Count", Req, Rep)
    sum = GrpcClient.stream_unary("Sum", Req, Rep)
    echo = GrpcClient.stream_stream("Echo", Req, Rep)
    fail = GrpcClient.unary("Fail", Req, Rep)


def make_app() -> GrpcApp:
    app = GrpcApp(tls=Insecure())
    app.add_service(make_service())
    return app


async def test_the_four_call_kinds() -> None:
    async with TestClient(make_app()) as tc:
        calc = tc.client(CalcClient)
        assert await calc.double(Req(n=21)) == Rep(total=42, text="/calc.v1.Calc/Double")
        assert [r.total async for r in calc.count(Req(n=3))] == [0, 1, 2]
        assert await calc.sum([Req(n=1), Req(n=2)]) == Rep(total=3)

        async def requests() -> AsyncIterator[Req]:
            yield Req(n=5, text="a")
            yield Req(n=6, text="b")

        echoed = [r async for r in calc.echo(requests())]
        assert echoed == [Rep(total=5, text="a"), Rep(total=6, text="b")]


async def test_a_bidirectional_stream_answers_before_the_requests_end() -> None:
    async with TestClient(make_app()) as tc:
        answers: asyncio.Queue[Rep] = asyncio.Queue()

        async def ping_pong() -> AsyncIterator[Req]:
            for i in range(50):
                yield Req(n=i)
                assert (await answers.get()).total == i  # sent only once answered

        async for answer in tc.client(CalcClient).echo(ping_pong()):
            answers.put_nowait(answer)


async def test_a_long_client_stream_arrives_whole_and_in_order() -> None:
    async with TestClient(make_app()) as tc:
        requests = [Req(n=i) for i in range(5000)]
        assert await tc.client(CalcClient).sum(requests) == Rep(total=sum(range(5000)))


class FakeTransport:
    """A `TransportContext` without `grpc`."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def time_remaining(self) -> float | None:
        return None

    def invocation_metadata(self) -> Pairs:
        return ()

    def peer(self) -> str:
        return "fake"

    def peer_identities(self) -> None:
        return None

    def auth_context(self) -> dict[str, list[bytes]]:
        return {}

    async def send_initial_metadata(self, metadata: Pairs, /) -> None:
        pass

    def set_trailing_metadata(self, metadata: Pairs, /) -> None:
        pass

    async def write(self, message: bytes, /) -> None:
        self.written.append(message)


class CollectBackend:
    """A `TransportBackend` that only keeps the invokers."""

    def __init__(self) -> None:
        self.invokers: dict[str, Invoker] = {}

    def add_method(self, invoker: Invoker) -> None:
        self.invokers[invoker.path.rsplit("/", 1)[1]] = invoker

    async def start(self) -> int:
        return 0

    async def stop(self, grace: float | None) -> None:
        pass

    async def wait_for_termination(self) -> None:
        pass


async def test_invokers_answer_bytes_or_a_failure_without_grpc() -> None:
    app, backend, transport = make_app(), CollectBackend(), FakeTransport()
    schema = app._build_schema()
    app._register(backend, schema)
    req, rep = schema.codec(Req), schema.codec(Rep)
    call = {name: invoker.call for name, invoker in backend.invokers.items()}

    double = await call["Double"](req.encode(Req(n=21)), transport)  # with a Context
    assert isinstance(double, bytes)
    assert rep.decode(double) == Rep(total=42, text="/calc.v1.Calc/Double")
    assert await call["Count"](req.encode(Req(n=2)), transport) is None
    assert [rep.decode(m) for m in transport.written] == [Rep(total=0), Rep(total=1)]

    failure = await call["Fail"](req.encode(Req(text="grpc")), transport)  # the fast path
    assert isinstance(failure, RpcFailure)
    assert (failure.code, failure.message) == (StatusCode.FAILED_PRECONDITION, "not now")
    assert failure.details
    invalid = RpcFailure(StatusCode.INVALID_ARGUMENT, "invalid request payload")
    assert await call["Fail"](b"\xff", transport) == invalid


async def test_grpc_error_carries_standard_and_registered_details() -> None:
    async with TestClient(make_app()) as tc:
        with pytest.raises(GrpcError) as raised:
            await tc.client(CalcClient).fail(Req(text="grpc"))
    error = raised.value
    assert (error.code, error.message) == (StatusCode.FAILED_PRECONDITION, "not now")
    assert error.detail(ErrorInfo) == ErrorInfo(reason="BUSY", domain="calc", metadata={"k": "v"})
    assert error.detail(BadRequest) == BadRequest(
        field_violations=[FieldViolation(field="n", description="odd")]
    )
    assert error.detail(Quota) == Quota(left=3)
    # A detail the client does not know stays packed, with its standard type URL.
    unknown = error.detail(AnyMessage)
    assert unknown is not None
    assert unknown.type_url == "type.googleapis.com/fastgrpc3.Unregistered"


async def test_unexpected_exceptions_become_internal_and_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with TestClient(make_app()) as tc:
        calc = tc.client(CalcClient)
        with caplog.at_level(logging.ERROR, logger="fastgrpc3"):
            for text in ("boom", "bad response"):
                with pytest.raises(GrpcError) as raised:
                    await calc.fail(Req(text=text))
                assert raised.value.code == StatusCode.INTERNAL
                assert raised.value.message == "internal server error"
                assert raised.value.details == []
    assert "unhandled error in /calc.v1.Calc/Fail" in caplog.text
    assert "boom" not in str(raised.value)


async def test_a_dict_response_is_validated_into_the_model() -> None:
    async with TestClient(make_app()) as tc:
        assert await tc.client(CalcClient).fail(Req(text="dict")) == Rep(total=7)


async def test_exception_handlers_map_exceptions() -> None:
    app = make_app()

    @app.exception_handler(LookupError)
    async def not_found(error: Exception) -> GrpcError:
        return GrpcError(StatusCode.NOT_FOUND, f"no {error}")

    async with TestClient(app) as tc:
        with pytest.raises(GrpcError) as raised:
            await tc.client(CalcClient).fail(Req(text="custom", n=4))
    assert (raised.value.code, raised.value.message) == (StatusCode.NOT_FOUND, "no 4")


async def test_invalid_request_bytes_give_invalid_argument() -> None:
    async with TestClient(make_app()) as tc:
        call: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes]
        call = tc.channel.grpc_channel.unary_unary("/calc.v1.Calc/Double")
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await call(b"\x08")  # truncated varint
    assert raised.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert raised.value.details() == "invalid request payload"


async def test_hooks_run_before_decoding_and_can_refuse() -> None:
    app = make_app()
    seen: list[str] = []

    async def authenticate(ctx: Context) -> None:
        seen.append(ctx.method)
        if ctx.metadata.get("x-user") != "alice":
            raise GrpcError(StatusCode.UNAUTHENTICATED, "who are you?")

    app.add_interceptor(authenticate)
    async with TestClient(app) as tc:
        assert await tc.client(CalcClient).double(Req(n=1), metadata={"x-user": "alice"})
        call: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes]
        call = tc.channel.grpc_channel.unary_unary("/calc.v1.Calc/Double")
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await call(b"\x08")  # never decoded: refused first
    assert raised.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert seen == ["/calc.v1.Calc/Double", "/calc.v1.Calc/Double"]


async def test_middlewares_wrap_the_handler_in_order() -> None:
    app = make_app()
    events: list[str] = []

    @app.middleware
    async def outer(ctx: Context, call_next: object) -> object:
        events.append("outer in")
        result = await call_next()  # type: ignore[operator]
        events.append("outer out")
        return result

    @app.middleware
    async def inner(ctx: Context, call_next: object) -> object:
        events.append(f"inner {ctx.method}")
        result = await call_next()  # type: ignore[operator]
        return Rep(total=result.total + 1)

    async with TestClient(app) as tc:
        assert (await tc.client(CalcClient).double(Req(n=1))).total == 3
    assert events == ["outer in", "inner /calc.v1.Calc/Double", "outer out"]


async def test_middlewares_wrap_a_whole_server_stream() -> None:
    """A server stream is sent inside `call_next()`: the middleware sees it end, and its
    errors."""
    app = GrpcApp(tls=Insecure())
    service = GrpcService("Calc", package="calc.v1")
    events: list[str] = []

    @service.unary_stream("Count")
    async def count(req: Req) -> AsyncIterator[Rep]:
        for i in range(req.n):
            events.append(f"sent {i}")
            yield Rep(total=i)
        if req.text == "fail":
            raise RuntimeError("late")

    app.add_service(service)

    @app.middleware
    async def watch(ctx: Context, call_next: object) -> object:
        events.append("in")
        try:
            result = await call_next()  # type: ignore[operator]
        except Exception as e:
            events.append(f"error {e}")
            raise
        events.append(f"out {result}")
        return result

    async with TestClient(app) as tc:
        calc = tc.client(CalcClient)
        assert [r.total async for r in calc.count(Req(n=2))] == [0, 1]
        assert events == ["in", "sent 0", "sent 1", "out None"]

        events.clear()
        with pytest.raises(GrpcError) as raised:
            [r async for r in calc.count(Req(n=1, text="fail"))]
        assert raised.value.code == StatusCode.INTERNAL
        assert events == ["in", "sent 0", "error late"]


async def test_requests_and_responses_without_validation() -> None:
    class Positive(BaseModel):
        n: int = Field(default=1, ge=1)

    class PositiveClient(GrpcClient):
        package = "calc.v1"
        service = "Checked"
        echo = GrpcClient.unary("Echo", Positive, Positive)

    service = GrpcService("Checked", package="calc.v1")

    @service.unary()
    async def Echo(req: Positive) -> Positive:
        return Positive.model_construct(n=req.n - 10)  # sent as is: an exact Positive

    for validate in (True, False):
        app = GrpcApp(tls=Insecure(), validate_requests=validate)
        app.add_service(service)
        async with TestClient(app) as tc:
            trusting = PositiveClient(tc.channel, validate_responses=False)
            if validate:
                with pytest.raises(GrpcError) as raised:
                    await trusting.echo(Positive.model_construct(n=-1))
                assert raised.value.code == StatusCode.INVALID_ARGUMENT
                with pytest.raises(GrpcError) as raised:
                    await tc.client(PositiveClient).echo(Positive(n=5))
                assert raised.value.message == "invalid response payload"
            else:
                assert (await trusting.echo(Positive.model_construct(n=-1))).n == -11


async def test_numbers_assigned_without_a_lock_are_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Numbered(BaseModel):
        a: int = field(1, default=0)

    class Unnumbered(BaseModel):
        a: int = 0
        b: str = ""

    for model, expected in ((Numbered, []), (Unnumbered, ["Unnumbered.a", "Unnumbered.b"])):
        service = GrpcService("Warn", package="warn.v1")

        async def Echo(req: model) -> model:  # type: ignore[valid-type]
            return req

        service.unary()(Echo)
        app = GrpcApp(tls=Insecure())
        app.add_service(service)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="fastgrpc3"):
            async with TestClient(app):
                pass
        warnings = [r.getMessage() for r in caplog.records if "declaration order" in r.message]
        assert [name for name in expected if any(name in w for w in warnings)] == expected
        assert bool(warnings) == bool(expected)


async def test_lifespan_wraps_the_server() -> None:
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: GrpcApp) -> AsyncIterator[None]:
        events.append("up")
        yield
        events.append("down")

    app = GrpcApp(tls=Insecure(), lifespan=lifespan)
    app.add_service(make_service())
    async with TestClient(app):
        assert events == ["up"]
    assert events == ["up", "down"]


async def test_the_compiled_contract_is_the_exported_one() -> None:
    app = make_app()
    async with TestClient(app):
        assert app.schema is not None
        text = app.schema.files["calc/v1/calc.proto"]
    assert "  rpc Count(Req) returns (stream Rep);\n" in text
    assert "  rpc Echo(stream Req) returns (stream Rep);\n" in text


def test_the_transport_is_explicit() -> None:
    with pytest.raises(ValueError, match="choose the transport explicitly"):
        GrpcApp()


async def test_a_started_app_refuses_new_hooks() -> None:
    app = make_app()
    async with TestClient(app):
        with pytest.raises(RuntimeError, match="already started"):
            app.add_interceptor(lambda ctx: None)  # type: ignore[arg-type,return-value]
        with pytest.raises(RuntimeError, match="already started"):
            app.add_service(GrpcService("Other", package="other.v1"))
        with pytest.raises(RuntimeError, match="already started"):
            app.add_model(Req)


def test_services_are_added_once() -> None:
    app = make_app()
    with pytest.raises(ValueError, match="added twice"):
        app.add_service(make_service())


class Named(BaseModel):
    name: str = Field(min_length=1)


async def test_the_server_validates_requests() -> None:
    service = GrpcService("Names", package="names.v1")

    @service.unary()
    async def Greet(req: Named) -> Named:
        return req

    class NamesClient(GrpcClient):
        package = "names.v1"
        service = "Names"
        greet = GrpcClient.unary("Greet", Named, Named)

    app = GrpcApp(tls=Insecure())
    app.add_service(service)
    async with TestClient(app) as tc:
        names = tc.client(NamesClient)
        assert await names.greet(Named(name="a")) == Named(name="a")
        with pytest.raises(GrpcError) as raised:
            await names.greet(Named.model_construct(name=""))  # skips client-side checks
    assert (raised.value.code, raised.value.message) == (
        StatusCode.INVALID_ARGUMENT,
        "invalid request payload",
    )


async def test_run_stops_on_sigterm() -> None:
    import asyncio
    import os
    import signal

    app = GrpcApp(tls=Insecure(), address="127.0.0.1:0")
    app.add_service(make_service())
    running = asyncio.ensure_future(app.run(grace=0.1))
    while app.bound_port is None:
        await asyncio.sleep(0.01)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(running, timeout=5)
    assert app._server is None


async def test_a_busy_port_is_refused() -> None:
    first = make_app()
    first.address = "127.0.0.1:0"
    async with first.serving():
        second = make_app()
        second.address = f"127.0.0.1:{first.bound_port}"
        with pytest.raises(RuntimeError, match=r"(?i)bind"):
            await second.start()
