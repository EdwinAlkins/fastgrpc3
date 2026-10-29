"""The standard protos shipped in `fastgrpc3._proto` are generated and up to date."""

import importlib.util
from pathlib import Path

import fastgrpc3._proto
from fastgrpc3.codegen.generate import check

SCRIPT = Path(__file__).parent.parent / "scripts" / "generate_standard_protos.py"


def test_generated_standard_protos_are_up_to_date() -> None:
    spec = importlib.util.spec_from_file_location("generate_standard_protos", SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    output = Path(fastgrpc3._proto.__file__).parent
    assert check(script.standard_files(), output) == [], (
        "run `uv run python scripts/generate_standard_protos.py`"
    )
