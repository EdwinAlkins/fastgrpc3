"""`fastgrpc3` command line.

- `generate`: `.proto` files -> Python models, services and descriptor (proto first).
- `run`: serve an app, optionally in several worker processes.
- `openapi`: the OpenAPI document of an app's RPCs (what `app.enable_docs` serves).
- `export`, `lock`, `check`: code-first contracts. TARGET is `module:attribute`, naming a
  `SchemaRegistry` or an object with a `schema_registry` attribute (the application).
- `breaking`: the changes since a git ref (or a directory of `.proto` files) that break
  existing peers.
"""

import argparse
import importlib
import json
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import protoruf

from fastgrpc3.api.app import GrpcApp
from fastgrpc3.codegen.generate import CodegenError, check, generate, write
from fastgrpc3.ops.openapi import openapi
from fastgrpc3.runtime.workers import serve
from fastgrpc3.schema.breaking import breaking_changes
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
    value = _load(target)
    registry = getattr(value, "schema_registry", value)
    if not isinstance(registry, SchemaRegistry):
        raise CliError(f"{target} is not a SchemaRegistry")
    return registry


def load_app(target: str) -> GrpcApp:
    """Import `module:attribute` and return its `GrpcApp`."""
    app = _load(target)
    if not isinstance(app, GrpcApp):
        raise CliError(f"{target} is not a GrpcApp")
    return app


def _load(target: str) -> object:
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
        return getattr(module, attribute)
    except AttributeError as e:
        raise CliError(f"{module_name} has no attribute {attribute!r}") from e


def _openapi(args: argparse.Namespace) -> int:
    app = load_app(args.target)
    if app.docs is not None:
        document = app.docs.openapi()
    else:
        document = openapi(app.services, title=app.title)
    text = json.dumps(document, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}")
    return 0


def _run(args: argparse.Namespace) -> int:
    try:
        return serve(load_app, args.target, args.address, args.workers, args.grace)
    except ValueError as e:
        raise CliError(str(e)) from e


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


def _breaking(args: argparse.Namespace) -> int:
    registry, lock_path = load_registry(args.target), Path(args.lock)
    current = Schema.from_registry(registry, Lock.load(lock_path), unlocked="error")
    previous = protoruf.Schema(_previous_descriptor(args.against, args.target, args.lock))
    findings = breaking_changes(previous, current.info)
    for finding in findings:
        print(finding)
    count = sum(finding.level == "breaking" for finding in findings)
    if count:
        print(f"{count} breaking change(s) since {args.against}", file=sys.stderr)
        return 1
    print(f"no breaking change since {args.against}")
    return 0


def _previous_descriptor(against: str, target: str, lock: str) -> bytes:
    """The contract at `against`: a directory of `.proto` files, or a git ref whose code is
    exported in a temporary worktree (with this fastgrpc3)."""
    if Path(against).is_dir():
        return _compile_directory(Path(against))
    top = _git("rev-parse", "--show-toplevel").strip()
    if _git_status("rev-parse", "--verify", "--quiet", f"{against}^{{commit}}") != 0:
        raise CliError(f"{against} is neither a directory nor a git ref")
    here = Path.cwd().resolve().relative_to(Path(top).resolve())
    with tempfile.TemporaryDirectory(prefix="fastgrpc3-breaking-") as tmp:
        worktree, output = Path(tmp) / "tree", Path(tmp) / "proto"
        _git("worktree", "add", "--detach", "--quiet", str(worktree), against)
        try:
            command = [sys.executable, "-m", "fastgrpc3", "export", target]
            command += ["-o", str(output), "--lock", lock]
            done = subprocess.run(command, cwd=worktree / here, capture_output=True, text=True)
            if done.returncode != 0:
                reason = done.stderr.strip().splitlines()[-1:] or ["failed"]
                raise CliError(f"cannot export {target} at {against}: {reason[0]}")
            return _compile_directory(output)
        finally:
            _git("worktree", "remove", "--force", str(worktree))


def _compile_directory(directory: Path) -> bytes:
    roots = sorted(str(path.relative_to(directory)) for path in directory.rglob("*.proto"))
    if not roots:
        raise CliError(f"no .proto file in {directory}")
    return protoruf.compile_protos(roots, [directory])


def _git(*arguments: str) -> str:
    done = subprocess.run(["git", *arguments], capture_output=True, text=True)
    if done.returncode != 0:
        raise CliError(f"git {arguments[0]}: {done.stderr.strip()}")
    return done.stdout


def _git_status(*arguments: str) -> int:
    return subprocess.run(["git", *arguments], capture_output=True).returncode


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

    run = commands.add_parser("run", help="serve an app", description="serve an app")
    run.add_argument("target", metavar="TARGET", help="module:attribute of a GrpcApp")
    run.add_argument("--address", help="host:port to bind (default: the app's)")
    run.add_argument("--workers", type=int, default=1, help="processes (default: 1)")
    run.add_argument("--grace", type=float, default=5.0, help="seconds to finish calls on stop")
    run.set_defaults(command=_run)

    openapi_help = "write the OpenAPI document of an app's RPCs"
    openapi_parser = commands.add_parser("openapi", help=openapi_help, description=openapi_help)
    openapi_parser.add_argument("target", metavar="TARGET", help="module:attribute of a GrpcApp")
    openapi_parser.add_argument("-o", "--output", help="file (default: standard output)")
    openapi_parser.set_defaults(command=_openapi)

    export = command("export", _export, "write the .proto files of the contract")
    export.add_argument("-o", "--output", default="proto", help="directory (default: %(default)s)")
    command("lock", _lock, "number new fields and update proto.lock")
    breaking = command("breaking", _breaking, "list the changes that break existing peers")
    breaking.add_argument(
        "--against", required=True, help="git ref, or directory of .proto files, to compare to"
    )
    command("check", _check, "fail if proto.lock does not match the models (for CI)")
    return parser
