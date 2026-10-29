"""Regenerate `src/fastgrpc3/_proto/` from the standard protos embedded in protoruf.

Run after upgrading protoruf: `uv run python scripts/generate_standard_protos.py`.
`tests/test_standard_protos.py` fails while the generated package is out of date.
"""

from pathlib import Path

from fastgrpc3.codegen.generate import generate, write

OUTPUT = Path(__file__).resolve().parent.parent / "src" / "fastgrpc3" / "_proto"
ROOTS = (
    "google/rpc/code.proto",
    "google/rpc/error_details.proto",
    "google/rpc/status.proto",
    "grpc/health/v1/health.proto",
    "grpc/reflection/v1/reflection.proto",
    "grpc/reflection/v1alpha/reflection.proto",
)


def standard_files() -> dict[str, str]:
    """The generated sources of the standard protos."""
    return generate([], roots=ROOTS)


if __name__ == "__main__":
    for path in write(standard_files(), OUTPUT):
        print(f"removed {path}")
    print(f"wrote {OUTPUT}")
