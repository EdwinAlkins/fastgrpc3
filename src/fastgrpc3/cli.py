"""`fastgrpc3` command line.

- `generate`: `.proto` files -> Python models, services and descriptor (proto first).
- `export`, `lock`, `check`: code-first contracts. TARGET is `module:attribute`, naming a
  `SchemaRegistry` or an object with a `schema_registry` attribute (the application).
"""

import argparse
import importlib
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastgrpc3.codegen.generate import CodegenError, check, generate, write
from fastgrpc3.schema.lock import Lock, LockError
from fastgrpc3.schema.registry import SchemaError, SchemaRegistry
from fastgrpc3.schema.schema import Schema

__all__ = ["main"]


class CliError(Exception):
    """A user error reported on stderr, without a traceback."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    args = _parser().parse_args(argv)
    try:
        status: int = args.command(args)
    except (CliError, SchemaError, LockError, CodegenError, RuntimeError) as e:
        # protoruf reports compilation errors as RuntimeError.
        print(f"error: {e}", file=sys.stderr)
        return 1
    return status


def load_registry(target: str) -> SchemaRegistry:
    """Import `module:attribute` and return its `SchemaRegistry`."""
    module_name, _, attribute = target.partition(":")
    if not module_name or not attribute:
        raise CliError(f"TARGET must be module:attribute, got {target!r}")
    # Like `python -m`: modules of the current directory are importable.
    sys.path.insert(0, str(Path.cwd()))
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise CliError(f"cannot import {module_name}: {e}") from e
    try:
        value = getattr(module, attribute)
    except AttributeError as e:
        raise CliError(f"{module_name} has no attribute {attribute!r}") from e
    registry = getattr(value, "schema_registry", value)
    if not isinstance(registry, SchemaRegistry):
        raise CliError(f"{target} is not a SchemaRegistry")
    return registry


def _generate(args: argparse.Namespace) -> int:
    config = _generate_config()
    proto_paths = args.proto_paths or config.get("proto_paths", [])
    if not proto_paths:
        raise CliError("give .proto directories, or set [tool.fastgrpc3.generate] proto_paths")
    output = args.output or config.get("output")
    if not output:
        raise CliError("give -o DIRECTORY, or set [tool.fastgrpc3.generate] output")
    include = args.include or config.get("include", [])
    files = generate([Path(p) for p in proto_paths], [Path(p) for p in include])
    if args.check:
        problems = check(files, Path(output))
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print("generated files are out of date: run `fastgrpc3 generate`", file=sys.stderr)
            return 1
        print(f"{output} is up to date")
        return 0
    for path in write(files, Path(output)):
        print(f"removed {path}")
    print(f"wrote {len(files)} files to {output}")
    return 0


def _generate_config() -> dict[str, Any]:
    """`[tool.fastgrpc3.generate]` of `./pyproject.toml`, if any."""
    path = Path("pyproject.toml")
    if not path.exists():
        return {}
    with path.open("rb") as stream:
        config: dict[str, Any] = tomllib.load(stream)
    section: dict[str, Any] = config.get("tool", {}).get("fastgrpc3", {}).get("generate", {})
    return section


def _export(args: argparse.Namespace) -> int:
    registry, lock_path = load_registry(args.target), Path(args.lock)
    schema = Schema.from_registry(registry, Lock.load(lock_path), unlocked="error")
    output = Path(args.output)
    for name, text in schema.files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    return 0


def _lock(args: argparse.Namespace) -> int:
    registry, lock_path = load_registry(args.target), Path(args.lock)
    schema = Schema.from_registry(registry, Lock.load(lock_path))
    schema.resolved.lock.save(lock_path)
    for change in schema.resolved.changes:
        print(change)
    breaking = [c for c in schema.resolved.changes if c.kind in ("removed", "renumbered")]
    if breaking:
        print(f"warning: {len(breaking)} wire-breaking change(s) for existing peers")
    print(f"wrote {lock_path}")
    return 0


def _check(args: argparse.Namespace) -> int:
    registry, lock_path = load_registry(args.target), Path(args.lock)
    previous = Lock.load(lock_path)
    schema = Schema.from_registry(registry, previous)
    if schema.resolved.lock.dumps() == previous.dumps():
        print(f"{lock_path} is up to date")
        return 0
    if not lock_path.exists():
        print(f"{lock_path} is missing: run `fastgrpc3 lock {args.target}`", file=sys.stderr)
        return 1
    print(f"{lock_path} is out of date, run `fastgrpc3 lock {args.target}`:", file=sys.stderr)
    for change in schema.resolved.changes:
        print(f"  {change}", file=sys.stderr)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fastgrpc3", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    def command(name: str, handler: object, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.add_argument("target", metavar="TARGET", help="module:attribute")
        sub.add_argument("--lock", default="proto.lock", help="lock file (default: %(default)s)")
        sub.set_defaults(command=handler)
        return sub

    generate_parser = commands.add_parser(
        "generate",
        help="generate Python models and services from .proto files",
        description="generate Python models and services from .proto files "
        "(defaults from [tool.fastgrpc3.generate] in pyproject.toml)",
    )
    generate_parser.add_argument("proto_paths", nargs="*", metavar="PROTO_PATH")
    generate_parser.add_argument("-o", "--output", help="output package directory")
    generate_parser.add_argument(
        "-I", "--include", action="append", help="extra import directory (repeatable)"
    )
    generate_parser.add_argument(
        "--check", action="store_true", help="fail if the generated files are not up to date"
    )
    generate_parser.set_defaults(command=_generate)

    export = command("export", _export, "write the .proto files of the contract")
    export.add_argument("-o", "--output", default="proto", help="directory (default: %(default)s)")
    command("lock", _lock, "number new fields and update proto.lock")
    command("check", _check, "fail if proto.lock does not match the models (for CI)")
    return parser
