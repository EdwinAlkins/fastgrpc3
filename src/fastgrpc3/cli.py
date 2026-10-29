"""`fastgrpc3` command line: `export`, `lock` and `check` for code-first contracts.

TARGET is `module:attribute`, naming a `SchemaRegistry` or an object with a
`schema_registry` attribute (the application, once it exists).
"""

import argparse
import importlib
import sys
from collections.abc import Sequence
from pathlib import Path

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
        registry = load_registry(args.target)
        lock_path = Path(args.lock)
        status: int = args.command(registry, lock_path, args)
    except (CliError, SchemaError, LockError, RuntimeError) as e:
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


def _export(registry: SchemaRegistry, lock_path: Path, args: argparse.Namespace) -> int:
    schema = Schema.from_registry(registry, Lock.load(lock_path), unlocked="error")
    output = Path(args.output)
    for name, text in schema.files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    return 0


def _lock(registry: SchemaRegistry, lock_path: Path, args: argparse.Namespace) -> int:
    schema = Schema.from_registry(registry, Lock.load(lock_path))
    schema.resolved.lock.save(lock_path)
    for change in schema.resolved.changes:
        print(change)
    breaking = [c for c in schema.resolved.changes if c.kind in ("removed", "renumbered")]
    if breaking:
        print(f"warning: {len(breaking)} wire-breaking change(s) for existing peers")
    print(f"wrote {lock_path}")
    return 0


def _check(registry: SchemaRegistry, lock_path: Path, args: argparse.Namespace) -> int:
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

    export = command("export", _export, "write the .proto files of the contract")
    export.add_argument("-o", "--output", default="proto", help="directory (default: %(default)s)")
    command("lock", _lock, "number new fields and update proto.lock")
    command("check", _check, "fail if proto.lock does not match the models (for CI)")
    return parser
