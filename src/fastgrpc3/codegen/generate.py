"""`fastgrpc3 generate`: `.proto` files -> Python package of models, services, descriptor.

Layout follows the proto files: `orders/v1/orders.proto` -> `orders/v1/orders_models.py`
(+ `orders_service.py` if it declares services). Imported files are generated too, except
the well-known types, which map to Python types. `_descriptor.py` at the root embeds the
compiled descriptor (without comments), so nothing is compiled at startup (decision D4).
"""

from collections.abc import Sequence
from pathlib import Path

import protoruf

from fastgrpc3.codegen.models import (
    HEADER,
    Index,
    is_generated,
    models_module,
    services_module,
)
from fastgrpc3.codegen.naming import module_parts
from fastgrpc3.codegen.source import bytes_literal

__all__ = ["CodegenError", "check", "generate", "write"]


class CodegenError(ValueError):
    """`.proto` input the generator does not handle (proto2, unknown types…)."""


def generate(
    proto_paths: Sequence[Path], include: Sequence[Path] = (), roots: Sequence[str] = ()
) -> dict[str, str]:
    """Generated files (relative POSIX path -> source) for `.proto` files or directories.

    `roots` adds import names resolved from `include` or from the standard protos embedded
    in protoruf (`"google/rpc/status.proto"`).
    """
    found, include_paths = _collect(proto_paths)
    roots = list(dict.fromkeys([*found, *roots]))
    if not roots:
        raise CodegenError("no .proto file found")
    paths = [*include_paths, *include]
    commented = protoruf.compile_protos(roots, paths, source_info=True)
    descriptor = protoruf.compile_protos(roots, paths)
    schema = protoruf.Schema(commented)

    files = sorted(name for name in schema.files if is_generated(name))
    for name in files:
        if schema.files[name].syntax != "proto3":
            raise CodegenError(f"{name}: only proto3 files are supported")
    index = Index.build(schema, files)
    output: dict[str, str] = {}
    try:
        for name in files:
            directories, stem = module_parts(name)
            base = "/".join((*directories, stem))
            output[f"{base}_models.py"] = models_module(index, name)
            if schema.files[name].services:
                output[f"{base}_service.py"] = services_module(index, name)
    except ValueError as e:
        raise CodegenError(str(e)) from e
    output["_descriptor.py"] = _descriptor_module(descriptor)
    for package in _packages(output):
        output[f"{package}/__init__.py" if package else "__init__.py"] = f"{HEADER}: do not edit.\n"
    return dict(sorted(output.items()))


def write(files: dict[str, str], output: Path) -> list[Path]:
    """Write the files and delete generated files no longer produced; return removed ones."""
    stale = _stale(files, output)
    for path in stale:
        path.unlink()
    for name, text in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    return stale


def check(files: dict[str, str], output: Path) -> list[str]:
    """Differences between `files` and `output`, as human-readable lines (empty: up to date)."""
    problems = []
    for name, text in files.items():
        path = output / name
        if not path.exists():
            problems.append(f"missing {path}")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"outdated {path}")
    problems += [f"stale {path}" for path in _stale(files, output)]
    return problems


def _collect(proto_paths: Sequence[Path]) -> tuple[list[str], list[Path]]:
    """Root import names, and the directories they are relative to."""
    roots: list[str] = []
    include_paths: list[Path] = []
    for path in proto_paths:
        if path.is_dir():
            include_paths.append(path)
            roots += sorted(p.relative_to(path).as_posix() for p in path.rglob("*.proto"))
        elif path.suffix == ".proto" and path.exists():
            include_paths.append(path.parent)
            roots.append(path.name)
        else:
            raise CodegenError(f"{path}: not a .proto file or a directory")
    return list(dict.fromkeys(roots)), list(dict.fromkeys(include_paths))


def _descriptor_module(descriptor: bytes) -> str:
    lines = [
        f"{HEADER}: do not edit.",
        '"""Compiled descriptor of the generated modules (without source info)."""',
        "",
        "DESCRIPTOR = (",
        *bytes_literal(descriptor),
        ")",
    ]
    return "\n".join(lines) + "\n"


def _packages(files: dict[str, str]) -> list[str]:
    packages = {""}
    for name in files:
        parts = name.split("/")[:-1]
        packages.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    return sorted(packages)


def _stale(files: dict[str, str], output: Path) -> list[Path]:
    if not output.is_dir():
        return []
    stale = []
    for path in sorted(output.rglob("*.py")):
        name = path.relative_to(output).as_posix()
        if name not in files and _is_generated_file(path):
            stale.append(path)
    return stale


def _is_generated_file(path: Path) -> bool:
    with path.open(encoding="utf-8") as stream:
        return stream.readline().startswith(HEADER)
