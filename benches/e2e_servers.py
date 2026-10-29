"""Echo servers for the end-to-end benchmark, one per mode, run in their own process.

    python benches/e2e_servers.py MODE PORT        # serve until killed
    python benches/e2e_servers.py MODE --prepare DIR   # write the protoset and request bytes

Modes: `raw` (`grpc.aio` with `google.protobuf` (upb) messages, i.e. generated stubs, no
framework), `fastgrpc2`, `fastgrpc3`, `fastgrpc3-novalidate` (`validate_requests=False`),
and `fastgrpc3-deps-N-KIND`: fastgrpc3 with N trivial dependencies per handler, each a
`sync` or `async` function or a `gen` / `agen` generator.
Every server echoes the request of `bench.v1.Bench/{Small,Medium,Large}`.
"""

import argparse
import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

from contract import SIZES, make_models, sample

SERVICE = "bench.v1.Bench"
MAX_CONCURRENT_RPCS = 1024


def method(size: str) -> str:
    return size.capitalize()


def echo_handler(model: Any) -> Any:
    """An echo handler annotated with `model` (one function per model: annotations are
    evaluated lazily, a loop variable would be read too late)."""

    async def echo(request: model) -> model:  # type: ignore[valid-type]
        return request

    return echo


DEPS_MODE = re.compile(r"fastgrpc3-deps-(\d+)-(sync|async|gen|agen)")


def is_mode(mode: str) -> bool:
    simple = ("raw", "fastgrpc2", "fastgrpc3", "fastgrpc3-novalidate")
    return mode in simple or DEPS_MODE.fullmatch(mode) is not None


def make_provider(kind: str) -> Callable[..., Any]:
    """A dependency that does nothing, of the given kind."""

    def sync() -> int:
        return 1

    async def async_() -> int:
        return 1

    def gen() -> Iterator[int]:
        yield 1

    async def agen() -> AsyncIterator[int]:
        yield 1

    providers: dict[str, Callable[..., Any]] = {
        "sync": sync,
        "async": async_,
        "gen": gen,
        "agen": agen,
    }
    return providers[kind]


def deps_handler(model: Any, count: int, kind: str) -> Any:
    """An echo handler with `count` dependencies of `kind`."""
    from fastgrpc3 import Depends

    async def echo(request: Any, **dependencies: int) -> Any:
        return request

    keyword = inspect.Parameter.KEYWORD_ONLY
    parameters = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    parameters += [
        inspect.Parameter(f"d{i}", keyword, default=Depends(make_provider(kind)))
        for i in range(count)
    ]
    echo.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    echo.__annotations__ = {"request": model, "return": model}
    return echo


def fastgrpc3_app(port: int, mode: str = "fastgrpc3") -> Any:
    from fastgrpc3 import GrpcApp, GrpcService, Insecure

    models = make_models()
    service = GrpcService("Bench", package="bench.v1")
    deps = DEPS_MODE.fullmatch(mode)
    for size in SIZES:
        if deps is None:
            handler = echo_handler(models.of(size))
        else:
            handler = deps_handler(models.of(size), int(deps[1]), deps[2])
        service.unary(method(size))(handler)
    validate = mode != "fastgrpc3-novalidate"
    app = GrpcApp(tls=Insecure(), address=f"127.0.0.1:{port}", validate_requests=validate)
    app.add_service(service)
    return app, models


def fastgrpc2_app(port: int) -> Any:
    from fastgrpc2 import GrpcApp, GrpcService, Insecure

    models = make_models()
    service = GrpcService("Bench", package="bench.v1")
    for size in SIZES:
        service.unary(method(size))(echo_handler(models.of(size)))
    app = GrpcApp("bench", address=f"127.0.0.1:{port}", tls=Insecure())
    app.add_service(service)
    return app, models


def fastgrpc3_descriptor() -> bytes:
    app, _ = fastgrpc3_app(0)
    from fastgrpc3 import Schema

    descriptor: bytes = Schema.from_registry(app.schema_registry).descriptor
    return descriptor


async def serve_raw(port: int) -> None:
    import grpc
    import grpc.aio
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    pool = descriptor_pool.DescriptorPool()
    for file in descriptor_pb2.FileDescriptorSet.FromString(fastgrpc3_descriptor()).file:
        pool.Add(file)
    service = pool.FindServiceByName(SERVICE)

    async def echo(request: Any, context: Any) -> Any:
        return request

    handlers = {}
    for rpc in service.methods:
        message_class = message_factory.GetMessageClass(rpc.input_type)
        handlers[rpc.name] = grpc.unary_unary_rpc_method_handler(
            echo,
            request_deserializer=message_class.FromString,
            response_serializer=message_class.SerializeToString,
        )
    server = grpc.aio.server(maximum_concurrent_rpcs=MAX_CONCURRENT_RPCS)
    server.add_registered_method_handlers(SERVICE, handlers)
    server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    await server.wait_for_termination()


def prepare(mode: str, directory: Path) -> None:
    """Write `bench.protoset` and `<size>.bin` (an encoded request) for `mode`."""
    import protoruf

    directory.mkdir(parents=True, exist_ok=True)
    if mode == "fastgrpc2":
        from fastgrpc2.core import codec

        app, models = fastgrpc2_app(0)
        text = app.export_proto()
        descriptor = protoruf.compile_protos(["bench.proto"], sources={"bench.proto": text})
        for size in SIZES:
            data = codec.serializer(models.of(size))(sample(models, size))
            (directory / f"{size}.bin").write_bytes(data)
    else:
        app, models = fastgrpc3_app(0)
        from fastgrpc3 import Schema

        schema = Schema.from_registry(app.schema_registry)
        descriptor = schema.descriptor
        for size in SIZES:
            data = schema.codec(models.of(size)).encode(sample(models, size))
            (directory / f"{size}.bin").write_bytes(data)
    (directory / "bench.protoset").write_bytes(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", help="raw, fastgrpc2, fastgrpc3 or fastgrpc3-deps-N-KIND")
    parser.add_argument("port", type=int, nargs="?", default=0)
    parser.add_argument("--prepare", type=Path, metavar="DIR")
    args = parser.parse_args()
    if not is_mode(args.mode):
        parser.error(f"unknown mode {args.mode}")
    if args.prepare:
        prepare(args.mode, args.prepare)
    elif args.mode == "raw":
        asyncio.run(serve_raw(args.port))
    elif args.mode == "fastgrpc2":
        app, _ = fastgrpc2_app(args.port)
        asyncio.run(app.run())
    else:
        app, _ = fastgrpc3_app(args.port, args.mode)
        asyncio.run(app.run())


if __name__ == "__main__":
    main()
