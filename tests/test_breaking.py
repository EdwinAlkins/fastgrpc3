"""`fastgrpc3 breaking`: changes between two contracts that break existing peers."""

import shutil
import subprocess
import sys
from pathlib import Path

import protoruf
import pytest

from fastgrpc3.cli import main
from fastgrpc3.schema.breaking import breaking_changes

OLD = """
syntax = "proto3";
package shop.v1;

enum Color { COLOR_UNSPECIFIED = 0; COLOR_RED = 1; COLOR_BLUE = 2; COLOR_GREEN = 3; }

message Item {
  string name = 1;
  int32 count = 2;
  sint32 delta = 3;
  repeated string tags = 4;
  string note = 5;
  string legacy = 6;
  Color color = 7;
  oneof price { int64 cents = 8; string text = 9; }
  int32 limit = 10;
  string sku = 11;
  reserved 20;
}

message Gone {}

service Shop {
  rpc Get(Item) returns (Item);
  rpc Watch(Item) returns (stream Item);
  rpc Drop(Item) returns (Item);
}
"""

NEW = """
syntax = "proto3";
package shop.v1;

enum Color { COLOR_UNSPECIFIED = 0; COLOR_RED = 1; COLOR_AZURE = 2; reserved 3; }

message Item {
  string title = 1;
  int64 count = 2;
  int32 delta = 3;
  string tags = 4;
  reserved 5;
  Color color = 7;
  int64 cents = 8;
  oneof price { string text = 9; }
  optional int32 limit = 10;
  bytes sku = 11;
  string again = 20;
}

service Shop {
  rpc Get(Item) returns (Color2);
  rpc Watch(Item) returns (Item);
}

message Color2 {}
"""


def view(source: str) -> protoruf.Schema:
    return protoruf.Schema(protoruf.compile_protos(["shop.proto"], sources={"shop.proto": source}))


def test_breaking_changes_follow_the_wire_rules() -> None:
    found = {(f.level, f.where, f.what) for f in breaking_changes(view(OLD), view(NEW))}
    item = "shop.v1.Item"
    assert found == {
        ("breaking", "shop.v1.Gone", "message removed"),
        ("breaking", f"{item}.delta", "type changed from sint32 to int32"),
        ("breaking", f"{item}.tags", "cardinality changed from repeated to singular"),
        ("breaking", f"{item}.legacy", "field 6 removed without reserving its number"),
        ("breaking", f"{item}.cents", "oneof changed from price to none"),
        ("breaking", f"{item}.again", "reuses number 20, reserved for a removed field"),
        ("breaking", "shop.v1.Shop/Get", "response changed from shop.v1.Item to shop.v1.Color2"),
        ("breaking", "shop.v1.Shop/Watch", "server side no longer streams"),
        ("breaking", "shop.v1.Shop/Drop", "method removed"),
        (
            "warning",
            f"{item}.name",
            "field 1 renamed to title (wire-compatible; JSON and code change)",
        ),
        (
            "warning",
            f"{item}.limit",
            "`optional` added (wire-compatible; presence and code change)",
        ),
        (
            "warning",
            "shop.v1.Color.COLOR_BLUE",
            "value 2 renamed to COLOR_AZURE (wire-compatible; JSON changes)",
        ),
    }
    # Not reported: int32 -> int64 (same varint), string -> bytes, a field deleted with its
    # number reserved (note = 5), an enum value deleted with its number reserved (GREEN = 3).


def test_breaking_changes_come_first() -> None:
    levels = [f.level for f in breaking_changes(view(OLD), view(NEW))]
    assert levels == sorted(levels, key=lambda level: level != "breaking")
    assert breaking_changes(view(OLD), view(OLD)) == []


MODULE = """
from pydantic import BaseModel
from fastgrpc3 import GrpcApp, GrpcService, Insecure
from fastgrpc3.types import field


class Req(BaseModel):
    name: str = field(1, default="")
    count: int = field(2, default=0)


service = GrpcService("Shop", package="shop.v1")


@service.unary()
async def Get(req: Req) -> Req:
    return req


app = GrpcApp(tls=Insecure())
app.add_service(service)
"""


def git(cwd: Path, *arguments: str) -> None:
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", *identity, *arguments], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "shop_app.py").write_text(MODULE)
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "v1")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_against_a_git_ref(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["breaking", "shop_app:app", "--against", "HEAD"]) == 0
    assert "no breaking change since HEAD" in capsys.readouterr().out

    changed = MODULE.replace("count: int = field(2", "count: str = field(2")
    (project / "shop_app.py").write_text(changed)
    _reload("shop_app")
    assert main(["breaking", "shop_app:app", "--against", "HEAD"]) == 1
    captured = capsys.readouterr()
    assert "breaking: shop.v1.Req.count: type changed from int64 to string" in captured.out
    assert "1 breaking change(s) since HEAD" in captured.err
    worktrees = subprocess.run(["git", "worktree", "list"], capture_output=True, text=True)
    assert len(worktrees.stdout.splitlines()) == 1  # the temporary one is gone


def test_against_a_directory(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["export", "shop_app:app", "-o", "before"]) == 0
    (project / "shop_app.py").write_text(MODULE.replace('name: str = field(1, default="")\n', ""))
    _reload("shop_app")
    assert main(["breaking", "shop_app:app", "--against", "before"]) == 1
    assert "shop.v1.Req.name: field 1 removed without reserving its number" in (
        capsys.readouterr().out
    )


def test_unknown_reference(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["breaking", "shop_app:app", "--against", "no-such-ref"]) == 1
    assert "neither a directory nor a git ref" in capsys.readouterr().err


def _reload(name: str) -> None:
    """Forget a module edited in place (its `.pyc` may look fresh within the same second)."""
    sys.modules.pop(name, None)
    shutil.rmtree(Path.cwd() / "__pycache__", ignore_errors=True)
