"""Dependency injection, as in FastAPI: `Depends(provider)` in a handler's parameters.

A provider may itself take `Depends(...)` and `Context` parameters. It can be a function, a
coroutine function, a (sync or async) generator whose code after `yield` runs once the
response is sent, a class (its `__init__` parameters are filled), an instance with a
`__call__` of any of these kinds, or a `functools.partial`. A generator sees the handler's
exception at its `yield`, but cannot swallow it: if it does not raise it again, the call
still fails. Within one call, a provider runs once and its value is shared
(`use_cache=False` opts out). The graph is resolved when the server starts, with
`dependency_overrides` applied, so a call does no introspection.

A `def` provider (function or generator) runs on the event loop: cheap, but while it
blocks, no other call of the process progresses. `@blocking` runs it in a thread instead
(a round trip costs tens of µs), for providers that do blocking I/O. Unmarked `def`
providers follow the app's `sync_providers` (`"loop"` by default, or `"threadpool"`), and
`@non_blocking` keeps a provider on the loop whatever the app says. A `def` provider that
holds the loop longer than `SLOW_PROVIDER_SECONDS` is reported once in the log.
"""

import asyncio
import contextlib
import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from fastgrpc3.api.context import Context

__all__ = [
    "SLOW_PROVIDER_SECONDS",
    "Dependency",
    "DependencyPlan",
    "Depends",
    "Parameter",
    "blocking",
    "inspect_parameters",
    "non_blocking",
    "plan_dependencies",
]

logger = logging.getLogger("fastgrpc3")

Provider = Callable[..., Any]

#: A `def` provider that holds the event loop longer than this is reported (once).
SLOW_PROVIDER_SECONDS = 0.01

_THREADPOOL = "__fastgrpc3_threadpool__"  # the mark left by @blocking / @non_blocking


@dataclass(frozen=True, slots=True)
class Dependency:
    """A parameter filled by calling `dependency` (what `Depends(...)` returns)."""

    dependency: Provider
    use_cache: bool = True


def Depends(dependency: Provider, *, use_cache: bool = True) -> Any:
    """Fill a parameter with what `dependency` returns (see the module docstring).

    Typed `Any`, like FastAPI's, so that `repo: Repo = Depends(get_repo)` type-checks.
    """
    return Dependency(dependency, use_cache)


def blocking[P: Provider](provider: P) -> P:
    """Decorator: this `def` provider blocks (I/O), run it in a thread."""
    return _mark(provider, threadpool=True)


def non_blocking[P: Provider](provider: P) -> P:
    """Decorator: this `def` provider is quick, run it on the event loop even when the app
    runs `def` providers in threads."""
    return _mark(provider, threadpool=False)


def _mark[P: Provider](provider: P, *, threadpool: bool) -> P:
    if _kind(provider) not in ("call", "generator"):
        raise TypeError(f"{_name(provider)}: only `def` providers run in threads")
    setattr(provider, _THREADPOOL, threadpool)  # the function itself: overrides still match
    return provider


@dataclass(frozen=True, slots=True)
class Parameter:
    """A parameter of a handler or provider: its name, and the annotation without
    `Annotated`, or the `Dependency` that fills it."""

    name: str
    annotation: Any
    depends: Dependency | None
    has_default: bool


def inspect_parameters(function: Callable[..., Any]) -> list[Parameter]:
    """The parameters of `function` (a class: of its `__init__`; a `partial`: those left),
    with annotations resolved. The framework passes arguments by name, so positional-only
    parameters, `*args` and `**kwargs` are refused."""
    try:
        signature = inspect.signature(function, eval_str=True)
    except (NameError, TypeError, ValueError) as e:
        raise TypeError(f"cannot read the signature of {_name(function)}: {e}") from e
    parameters = []
    for parameter in signature.parameters.values():
        if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            raise TypeError(
                f"{_name(function)}: parameter {parameter.name!r} cannot be passed by name"
            )
        annotation = parameter.annotation
        if annotation is inspect.Parameter.empty:
            annotation = Any
        depends = parameter.default if isinstance(parameter.default, Dependency) else None
        if get_origin(annotation) is Annotated:
            annotation, *extras = get_args(annotation)
            depends = depends or next((e for e in extras if isinstance(e, Dependency)), None)
        has_default = parameter.default is not inspect.Parameter.empty and depends is None
        parameters.append(Parameter(parameter.name, annotation, depends, has_default))
    return parameters


@dataclass(frozen=True, slots=True)
class _Node:
    """One provider call: its arguments are earlier nodes (by index) or the context."""

    provider: Provider
    arguments: tuple[tuple[str, int | None], ...]  # (parameter, node index or None=Context)
    kind: str  # "call", "async", "generator", "async_generator"
    threadpool: bool = False


@dataclass(frozen=True, slots=True)
class DependencyPlan:
    """Providers in call order, and which node fills each handler parameter."""

    nodes: tuple[_Node, ...]
    outputs: tuple[tuple[str, int], ...]

    @property
    def needs_context(self) -> bool:
        return any(index is None for node in self.nodes for _, index in node.arguments)

    @property
    def needs_cleanup(self) -> bool:
        return any(node.kind in ("generator", "async_generator") for node in self.nodes)

    async def resolve(
        self, context: Context | None, stack: contextlib.AsyncExitStack | None
    ) -> dict[str, Any]:
        """Run the providers; generator providers register their cleanup on `stack`."""
        values: list[Any] = []
        for node in self.nodes:
            kwargs = {
                name: context if index is None else values[index] for name, index in node.arguments
            }
            values.append(await _call(node, kwargs, stack))
        return {name: values[index] for name, index in self.outputs}


def plan_dependencies(
    dependencies: Mapping[str, Dependency],
    overrides: Mapping[Provider, Provider],
    *,
    threadpool: bool = False,
) -> DependencyPlan:
    """Resolve the graph of the handler parameters `dependencies` (name -> `Dependency`);
    `threadpool` is where unmarked `def` providers run."""
    builder = _PlanBuilder(overrides, threadpool)
    outputs = tuple((name, builder.node(depends, ())) for name, depends in dependencies.items())
    return DependencyPlan(tuple(builder.nodes), outputs)


class _PlanBuilder:
    def __init__(self, overrides: Mapping[Provider, Provider], threadpool: bool) -> None:
        self.overrides = overrides
        self.threadpool = threadpool
        self.nodes: list[_Node] = []
        self.cached: dict[Provider, int] = {}

    def node(self, depends: Dependency, path: tuple[Provider, ...]) -> int:
        try:
            provider = self.overrides.get(depends.dependency, depends.dependency)
            hash(provider)
        except TypeError:
            raise TypeError(f"dependency {_name(depends.dependency)} is not hashable") from None
        if depends.use_cache and provider in self.cached:
            return self.cached[provider]
        if provider in path:
            cycle = " -> ".join(_name(p) for p in (*path, provider))
            raise TypeError(f"dependency cycle: {cycle}")
        arguments: list[tuple[str, int | None]] = []
        for parameter in inspect_parameters(provider):
            if parameter.depends is not None:
                arguments.append((parameter.name, self.node(parameter.depends, (*path, provider))))
            elif parameter.annotation is Context:
                arguments.append((parameter.name, None))
            elif not parameter.has_default:
                raise TypeError(
                    f"dependency {_name(provider)}: parameter {parameter.name!r} is neither "
                    "Depends(...) nor Context"
                )
        kind = _kind(provider)
        threadpool = kind in ("call", "generator") and bool(
            getattr(provider, _THREADPOOL, self.threadpool)
        )
        self.nodes.append(_Node(provider, tuple(arguments), kind, threadpool))
        index = len(self.nodes) - 1
        if depends.use_cache:
            self.cached[provider] = index
        return index


def _kind(provider: Provider) -> str:
    target = _called(provider)
    if inspect.isasyncgenfunction(target):
        return "async_generator"
    if inspect.isgeneratorfunction(target):
        return "generator"
    if inspect.iscoroutinefunction(target):
        return "async"
    return "call"


def _called(provider: Provider) -> Provider:
    """What runs when `provider` is called: a callable instance's `__call__`."""
    if inspect.isclass(provider) or inspect.isroutine(provider):
        return provider
    if isinstance(provider, functools.partial):
        return provider  # `inspect` looks through partials
    call: Provider = type(provider).__call__  # its kind is the kind of the function
    return call


async def _call(
    node: _Node, kwargs: dict[str, Any], stack: contextlib.AsyncExitStack | None
) -> Any:
    if node.kind == "async":
        return await node.provider(**kwargs)
    if node.kind == "call":
        if node.threadpool:
            result = await asyncio.to_thread(node.provider, **kwargs)
        else:
            started = time.perf_counter()
            result = node.provider(**kwargs)
            _watch(node.provider, started)
        # A lambda returning a coroutine is still awaited, as in fastgrpc2.
        return await result if inspect.isawaitable(result) else result
    assert stack is not None  # plans with generators always get a stack
    if node.kind == "async_generator":
        async_manager = contextlib.asynccontextmanager(node.provider)(**kwargs)
        value = await async_manager.__aenter__()
        stack.push_async_exit(_propagating_async(async_manager.__aexit__))
        return value
    manager = contextlib.contextmanager(node.provider)(**kwargs)
    if node.threadpool:
        value = await asyncio.to_thread(manager.__enter__)
        in_thread = manager.__exit__
        stack.push_async_exit(_propagating_async(lambda *d: asyncio.to_thread(in_thread, *d)))
        return value
    started = time.perf_counter()
    value = manager.__enter__()
    _watch(node.provider, started)
    stack.push(_propagating(manager.__exit__, node.provider))
    return value


def _propagating(exit: Callable[..., bool | None], provider: Provider) -> Callable[..., bool]:
    """`exit`, run for its cleanup only: the exception it was given goes on."""

    def run(*details: Any) -> bool:
        started = time.perf_counter()
        exit(*details)
        _watch(provider, started)
        return False

    return run


def _propagating_async(
    exit: Callable[..., Awaitable[bool | None]],
) -> Callable[..., Awaitable[bool]]:
    """`exit`, run for its cleanup only: the exception it was given goes on."""

    async def run(*details: Any) -> bool:
        await exit(*details)
        return False

    return run


_reported: set[Provider] = set()


def _watch(provider: Provider, started: float) -> None:
    """Report, once, a `def` provider that held the event loop too long."""
    elapsed = time.perf_counter() - started
    if elapsed > SLOW_PROVIDER_SECONDS and provider not in _reported:
        _reported.add(provider)
        logger.warning(
            "dependency %s blocked the event loop for %.0f ms: make it `async def` or "
            "mark it @blocking to run it in a thread",
            _name(provider),
            elapsed * 1000,
        )


def _name(function: Callable[..., Any]) -> str:
    if isinstance(function, functools.partial):
        return f"partial({_name(function.func)})"
    name = getattr(function, "__qualname__", None)
    return name if isinstance(name, str) else f"{type(function).__qualname__} instance"
