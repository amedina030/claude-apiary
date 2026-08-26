"""Minimal YAML subset reader/writer for researcher.

Apiary is stdlib-only (see docs/standards/code-style.md), so PyYAML is
off-limits. This module parses only what researcher emits itself:

  - Top-level mapping of ``key: value`` pairs.
  - Scalar values are bare strings (trimmed), double/single-quoted strings,
    or empty.
  - Lists are either block-style::

        tags:
          - multiplayer
          - networking

    or empty flow-style ``tags: []``. Nested mappings are not supported.

Any input that falls outside this subset raises ``YamlParseError``.

Quoting is **symmetric**: ``dumps`` quotes a value that would otherwise be
ambiguous, and ``loads`` unquotes it again, so ``loads(dumps(x)) == x``. A
value is treated as quoted only when it *starts* with a quote character and
the matching closing quote ends the value; a value that merely contains a
quote (``say "hi"``) is kept verbatim. ``#`` starts a comment only at the
start of a line or after whitespace, so ``C# generics``, ``issue#12`` and
``https://example.com/a#frag`` survive a round trip intact.
"""
from __future__ import annotations

from typing import Any


class YamlParseError(ValueError):
    """Raised when input is not in the supported YAML subset."""

    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


def _strip_comment(text: str) -> str:
    """Drop a trailing ``#`` comment from an *unquoted* scalar.

    ``#`` only opens a comment at the start of the text or after whitespace,
    which is what YAML itself does. Without that rule ``C# generics`` becomes
    ``C`` and ``https://example.com/a#frag`` loses its fragment.
    """
    for pos, ch in enumerate(text):
        if ch == "#" and (pos == 0 or text[pos - 1] in " \t"):
            return text[:pos].rstrip()
    return text


def _unescape_double(body: str) -> str:
    """Inverse of the escaping ``_dump_scalar`` applies inside ``"``…``"``."""
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] in ('"', "\\"):
            out.append(body[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _closing_quote(text: str, quote: str) -> int:
    """Index of the quote that closes *text* (which starts with *quote*), or -1.

    Double quotes honour backslash escapes; single quotes use YAML's doubled
    ``''`` for a literal apostrophe.
    """
    i = 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def _parse_scalar(text: str) -> tuple[str, bool]:
    """Parse one scalar. Returns ``(value, was_quoted)``.

    A value counts as quoted only when it opens with ``"`` or ``'`` *and* the
    matching closing quote ends it (bar an optional trailing comment). Anything
    else — including a value that merely contains a quote — is taken verbatim,
    minus a trailing comment.
    """
    text = text.strip()
    if text[:1] in ('"', "'"):
        quote = text[0]
        end = _closing_quote(text, quote)
        if end != -1:
            rest = text[end + 1:].strip()
            if rest == "" or rest.startswith("#"):
                body = text[1:end]
                if quote == '"':
                    return _unescape_double(body), True
                return body.replace("''", "'"), True
    return _strip_comment(text), False


def loads(text: str) -> dict[str, Any]:
    """Parse a YAML subset document into a dict.

    Returns an empty dict for empty/whitespace-only input.
    """
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    lines = text.splitlines()
    for idx, raw in enumerate(lines, start=1):
        content = raw.strip()

        # Whole-line comment, or blank.
        if not content or content.startswith("#"):
            continue

        # Detect list item (indented, starts with ``- ``).
        leading = len(raw) - len(raw.lstrip(" "))

        if content.startswith("- ") or content == "-":
            if current_list is None:
                raise YamlParseError("list item without a parent key", idx)
            item = _parse_scalar(content[2:])[0] if content != "-" else ""
            current_list.append(item)
            continue

        # Otherwise this must be a ``key: value`` line at indent zero.
        if leading != 0:
            raise YamlParseError("unexpected indentation", idx)

        if ":" not in content:
            raise YamlParseError("expected 'key: value'", idx)

        key, _, value = content.partition(":")
        key = key.strip()

        if not key:
            raise YamlParseError("empty key", idx)

        scalar, quoted = _parse_scalar(value)

        if not quoted and scalar == "":
            # Block-style list or empty scalar follows.
            current_key = key
            current_list = []
            result[key] = current_list
        elif not quoted and scalar == "[]":
            current_key = key
            current_list = None
            result[key] = []
        else:
            current_key = key
            current_list = None
            result[key] = scalar

    return result


def _needs_quoting(value: str) -> bool:
    if value == "":
        return True
    if value != value.strip():
        # Leading/trailing whitespace is stripped on load, so it must be quoted
        # to survive the round trip.
        return True
    if value[0] in ("-", "?", ":", "[", "]", "{", "}", "&", "*", "!", "|", ">",
                    "'", '"', "%", "@", "`", "#"):
        return True
    if any(ch in value for ch in (":", "#")):
        # Conservative: colons and hashes inside values get quoted. ``loads``
        # would cope with most of them bare, but quoting is unambiguous and
        # symmetric — it is unquoted again on the way back in.
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
