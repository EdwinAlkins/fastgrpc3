"""Dependency injection, as in FastAPI: `Depends(provider)` in a handler's parameters.

A provider may itself take `Depends(...)` and `Context` parameters. It can be a function, a
coroutine function, or a (sync or async) generator whose code after `yield` runs once the
response is sent. Within one call, a provider runs once and its value is shared
(`use_cache=False` opts out). The graph is resolved when the server starts, with
`dependency_overrides` applied, so a call does no introspection.
"""

import contextlib
import inspect
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from fastgrpc3.api.context import Context

__all__ = [
    "Dependency",
    "DependencyPlan",
    "Depends",
    "Parameter",
    "inspect_parameters",
    "plan_dependencies",
]

Provider = Callable[..., Any]


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


@dataclass(frozen=True, slots=True)
class Parameter:
    """A parameter of a handler or provider: its name, and the annotation without
    `Annotated`, or the `Dependency` that fills it."""

    name: str
    annotation: Any
    depends: Dependency | None
    has_default: bool


def inspect_parameters(function: Callable[..., Any]) -> list[Parameter]:
    """The parameters of `function`, with annotations resolved."""
    try:
        hints = typing.get_type_hints(function, include_extras=True)
    except (NameError, TypeError) as e:
        raise TypeError(f"cannot resolve the annotations of {_name(function)}: {e}") from e
    parameters = []
    for parameter in inspect.signature(function).parameters.values():
        annotation = hints.get(parameter.name, Any)
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
    dependencies: Mapping[str, Dependency], overrides: Mapping[Provider, Provider]
) -> DependencyPlan:
    """Resolve the graph of the handler parameters `dependencies` (name -> `Dependency`)."""
    builder = _PlanBuilder(overrides)
    outputs = tuple((name, builder.node(depends, ())) for name, depends in dependencies.items())
    return DependencyPlan(tuple(builder.nodes), outputs)


class _PlanBuilder:
    def __init__(self, overrides: Mapping[Provider, Provider]) -> None:
        self.overrides = overrides
        self.nodes: list[_Node] = []
        self.cached: dict[Provider, int] = {}

    def node(self, depends: Dependency, path: tuple[Provider, ...]) -> int:
        provider = self.overrides.get(depends.dependency, depends.dependency)
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
        self.nodes.append(_Node(provider, tuple(arguments), _kind(provider)))
        index = len(self.nodes) - 1
        if depends.use_cache:
            self.cached[provider] = index
        return index


def _kind(provider: Provider) -> str:
    if inspect.isasyncgenfunction(provider):
        return "async_generator"
    if inspect.isgeneratorfunction(provider):
        return "generator"
    if inspect.iscoroutinefunction(provider):
        return "async"
    return "call"


async def _call(
    node: _Node, kwargs: dict[str, Any], stack: contextlib.AsyncExitStack | None
) -> Any:
    if node.kind == "async":
        return await node.provider(**kwargs)
    if node.kind == "call":
        result = node.provider(**kwargs)
        # A lambda returning a coroutine is still awaited, as in fastgrpc2.
        return await result if inspect.isawaitable(result) else result
    assert stack is not None  # plans with generators always get a stack
    if node.kind == "async_generator":
        manager = contextlib.asynccontextmanager(node.provider)(**kwargs)
        return await stack.enter_async_context(manager)
    return stack.enter_context(contextlib.contextmanager(node.provider)(**kwargs))


def _name(function: Callable[..., Any]) -> str:
    return getattr(function, "__qualname__", repr(function))
