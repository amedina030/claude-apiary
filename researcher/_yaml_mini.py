"""Minimal YAML subset reader/writer for researcher.

Apiary is stdlib-only (see docs/standards/code-style.md), so PyYAML is
off-limits. This module parses only what researcher emits itself:

  - Top-level mapping of ``key: value`` pairs.
  - Scalar values are bare strings (trimmed) or empty.
  - Lists are either block-style::

        tags:
          - multiplayer
          - networking

    or empty flow-style ``tags: []``. Nested mappings are not supported.

Any input that falls outside this subset raises ``YamlParseError``.
"""
from __future__ import annotations

from typing import Any


class YamlParseError(ValueError):
    """Raised when input is not in the supported YAML subset."""

    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


def loads(text: str) -> dict[str, Any]:
    """Parse a YAML subset document into a dict.

    Returns an empty dict for empty/whitespace-only input.
    """
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    lines = text.splitlines()
    for idx, raw in enumerate(lines, start=1):
        stripped = raw.rstrip()

        # Strip comments (only when not part of a quoted string, which we don't
        # support anyway — so a bare ``#`` anywhere starts a comment).
        hash_pos = stripped.find("#")
        if hash_pos != -1:
            stripped = stripped[:hash_pos].rstrip()

        if not stripped:
            continue

        # Detect list item (indented, starts with ``- ``).
        leading = len(raw) - len(raw.lstrip(" "))
        content = stripped.strip()

        if content.startswith("- ") or content == "-":
            if current_list is None:
                raise YamlParseError("list item without a parent key", idx)
            item = content[2:].strip() if content != "-" else ""
            current_list.append(item)
            continue

        # Otherwise this must be a ``key: value`` line at indent zero.
        if leading != 0:
            raise YamlParseError("unexpected indentation", idx)

        if ":" not in content:
            raise YamlParseError("expected 'key: value'", idx)

        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()

        if not key:
            raise YamlParseError("empty key", idx)

        if value == "":
            # Block-style list or empty scalar follows.
            current_key = key
            current_list = []
            result[key] = current_list
        elif value == "[]":
            current_key = key
            current_list = None
            result[key] = []
        else:
            current_key = key
            current_list = None
            result[key] = value

    return result


def _needs_quoting(value: str) -> bool:
    if value == "":
        return True
    if value[0] in ("-", "?", ":", "[", "]", "{", "}", "&", "*", "!", "|", ">",
                    "'", '"', "%", "@", "`", "#"):
        return True
    if any(ch in value for ch in (":", "#")):
        # Conservative: colons and hashes inside values get quoted.
        return True
    return False


def _dump_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _needs_quoting(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def dumps(data: dict[str, Any]) -> str:
    """Emit a YAML subset document for *data*.

    Keys are preserved in insertion order. Lists render block-style
    (``  - item``) or ``[]`` when empty.
    """
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {_dump_scalar(item)}")
        else:
            lines.append(f"{key}: {_dump_scalar(value)}")
    return "\n".join(lines) + "\n"
