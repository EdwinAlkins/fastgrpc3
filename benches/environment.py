"""Benchmark context recorded with every result, and result tables."""

import importlib.metadata
import platform
import subprocess
import sys
from typing import Any

__all__ = ["environment", "markdown_table"]

PACKAGES = ("fastgrpc3", "protoruf", "grpcio", "pydantic", "protobuf", "fastgrpc2")


def environment() -> dict[str, Any]:
    """Machine, Python and library versions."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu": _cpu(),
        "packages": {name: _version(name) for name in PACKAGES},
    }


def markdown_table(title: str, columns: list[str], rows: dict[str, dict[str, float]]) -> str:
    """Rows as a Markdown table: one row per key, one column per measure."""
    lines = [f"### {title}", "", "| | " + " | ".join(rows) + " |"]
    lines.append("|---|" + "---:|" * len(rows))
    for column in columns:
        cells = [_format(values.get(column)) for values in rows.values()]
        lines.append(f"| {column} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _format(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 100:
        return f"{value:,.0f}"
    return f"{value:.2f}"


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _cpu() -> str:
    try:
        output = subprocess.run(
            ["lscpu"], capture_output=True, text=True, check=True, env={"LC_ALL": "C"}
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return platform.processor()
    for line in output.splitlines():
        if line.startswith("Model name:"):
            return line.split(":", 1)[1].strip()
    return platform.processor()
