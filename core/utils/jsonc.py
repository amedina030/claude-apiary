"""JSONC parser — stdlib-only JSON-with-comments + trailing-commas loader.

Strips ``//`` line comments, ``/* ... */`` block comments, and trailing
commas before ``]``/``}``, then delegates to :func:`json.loads`. Comments
inside string literals are preserved; escaped quotes within strings are
tracked so ``"//not a comment"`` parses correctly. Stripped characters
are replaced with spaces (or the original newline) so line/column numbers
in :class:`JsoncParseError` match the source file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsoncParseError(Exception):
    """Raised when JSONC parsing fails. Carries file path + line number."""

    def __init__(self, message: str, *, path: Path | None = None, line: int | None = None):
        self.path = path
        self.line = line
        prefix = ""
        if path is not None:
            prefix = f"{path}"
            if line is not None:
                prefix = f"{prefix}:{line}"
            prefix = f"{prefix}: "
        super().__init__(f"{prefix}{message}")


def loads(text: str, *, path: Path | None = None) -> Any:
    """Parse a JSONC string. Raises :class:`JsoncParseError` on failure."""
    stripped = _strip_comments_and_trailing_commas(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise JsoncParseError(exc.msg, path=path, line=exc.lineno) from exc


def load(path: Path) -> Any:
    """Read a file as JSONC. Raises :class:`JsoncParseError` on failure."""
    text = path.read_text(encoding="utf-8")
    return loads(text, path=path)


def _strip_comments_and_trailing_commas(text: str) -> str:
    """Replace comments with spaces and drop trailing commas before ] or }.

    Line/column positions are preserved so downstream JSON errors still
    point at the right source line. String literals (double-quoted) are
    scanned through unchanged, with backslash-escape awareness.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and nxt == "*":
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        out.append(ch)
        i += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(text: str) -> str:
    """Remove commas that directly precede ``]`` or ``}`` (ignoring whitespace).

    Preserves column positions by replacing the comma with a space. Scans
    string-literal state so commas inside strings are untouched.
    """
    out = list(text)
    n = len(out)
    in_string = False
    i = 0
    while i < n:
        ch = out[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and out[j] in " \t\r\n":
                j += 1
            if j < n and out[j] in "]}":
                out[i] = " "
        i += 1
    return "".join(out)
