"""Helpers that write Python source the way `ruff format` and isort lay it out."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

__all__ = [
    "INDENT",
    "LINE_LENGTH",
    "Imports",
    "blank_separated",
    "bytes_literal",
    "call",
    "docstring",
]

LINE_LENGTH = 100
INDENT = "    "


def call(prefix: str, args: list[str], suffix: str = "", indent: str = "") -> list[str]:
    """`prefix(arg, ...)suffix`, split over several lines when it does not fit."""
    one_line = f"{indent}{prefix}({', '.join(args)}){suffix}"
    if len(one_line) <= LINE_LENGTH:
        return [one_line]
    inner = f"{indent}{INDENT}{', '.join(args)}"
    if len(inner) <= LINE_LENGTH:
        return [f"{indent}{prefix}(", inner, f"{indent}){suffix}"]
    return [
        f"{indent}{prefix}(",
        *(f"{indent}{INDENT}{arg}," for arg in args),
        f"{indent}){suffix}",
    ]


def docstring(text: str, indent: str = "") -> list[str]:
    """A docstring holding `text` (a proto comment)."""
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if escaped.endswith('"'):
        escaped += " "
    lines = escaped.splitlines()
    if len(lines) == 1:
        return [f'{indent}"""{lines[0]}"""']
    body = [f"{indent}{line}".rstrip() for line in lines[1:]]
    return [f'{indent}"""{lines[0]}', *body, f'{indent}"""']


def bytes_literal(data: bytes, indent: str = INDENT) -> list[str]:
    """Lines of implicitly concatenated `b"..."` literals holding `data`."""
    width = LINE_LENGTH - len(indent) - len('b""')
    lines: list[str] = []
    current = ""
    for byte in data:
        piece = _escape(byte)
        if len(current) + len(piece) > width:
            lines.append(f'{indent}b"{current}"')
            current = ""
        current += piece
    if current or not lines:
        lines.append(f'{indent}b"{current}"')
    return lines


def _escape(byte: int) -> str:
    if byte == ord('"'):
        # Not `\"`: ruff would switch the literal to single quotes.
        return "\\x22"
    if byte == ord("\\"):
        return "\\\\"
    if 0x20 <= byte < 0x7F:
        return chr(byte)
    return f"\\x{byte:02x}"


@dataclass
class Imports:
    """Imports of a generated module; `use()` returns the local name of a symbol.

    A symbol whose name is taken by a generated class is imported under an alias.
    """

    taken: set[str]
    modules: set[str] = field(default_factory=set)
    names: dict[tuple[str, str], str] = field(default_factory=dict)
    relative: dict[str, tuple[int, str, str]] = field(default_factory=dict)

    def module(self, name: str) -> str:
        """`import name`; returns `name`."""
        self.modules.add(name)
        return name

    def use(self, module: str, name: str) -> str:
        """`from module import name`, aliased if needed; returns the local name."""
        if (module, name) not in self.names:
            local = name
            while local in self.taken:
                local += "_"
            self.names[(module, name)] = local
        return self.names[(module, name)]

    def relative_module(self, dots: int, package: str, module: str, alias: str) -> str:
        """`from <dots><package> import module as alias`; returns the alias."""
        self.relative[alias] = (dots, package, module)
        return alias

    def lines(self) -> list[str]:
        """Import statements in isort order: future, stdlib, third party, relative."""
        stdlib = [f"import {name}" for name in sorted(self.modules)]
        from_lines: dict[str, list[str]] = {}
        for (module, name), local in self.names.items():
            entry = name if local == name else f"{name} as {local}"
            from_lines.setdefault(module, []).append(entry)
        stdlib += [_from(m, from_lines.pop(m)) for m in sorted(from_lines) if m in _STDLIB]
        third_party = [_from(m, from_lines[m]) for m in sorted(from_lines)]
        relative = [
            _from("." * dots + package, [f"{module} as {alias}"])
            for alias, (dots, package, module) in sorted(
                self.relative.items(), key=lambda item: (-item[1][0], item[1][1], item[1][2])
            )
        ]
        sections = [["from __future__ import annotations"], stdlib, third_party, relative]
        return _join(section for section in sections if section)


_STDLIB = frozenset({"typing"})


def _from(module: str, entries: list[str]) -> str:
    entries = sorted(entries, key=_isort_key)
    line = f"from {module} import {', '.join(entries)}"
    if len(line) <= LINE_LENGTH:
        return line
    body = "\n".join(f"{INDENT}{entry}," for entry in entries)
    return f"from {module} import (\n{body}\n)"


def _isort_key(entry: str) -> tuple[int, str]:
    name = entry.split(" as ")[0]
    if name.isupper() and len(name) > 1:
        return 0, name
    return (1 if name[:1].isupper() else 2), name


def _join(sections: Iterable[list[str]]) -> list[str]:
    lines: list[str] = []
    for section in sections:
        if lines:
            lines.append("")
        lines += section
    return lines


def blank_separated(blocks: Iterable[list[str]], blank_lines: int = 2) -> Iterator[str]:
    """Blocks separated by `blank_lines` empty lines (two between top-level classes)."""
    for index, block in enumerate(blocks):
        if index:
            yield from [""] * blank_lines
        yield from block
