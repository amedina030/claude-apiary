"""One frontmatter dialect for the whole toolkit (Phase 3.3).

Apiary is stdlib-only (see ``docs/standards/code-style.md``), so PyYAML is
off-limits and every subsystem that wanted "markdown with YAML frontmatter"
grew its own parser. The 2026-08 deep review counted five
(``docs/review/subsystems/knowledge.md`` §3, "How many frontmatter/YAML
parsers?") and verified that two of them emit shapes the others cannot read:
``loads('tags: [a, b]')`` returned the *string* ``'[a, b]'`` in one, and a
block list came back as ``''`` in the other. This module is the single
dialect they all speak now.

The dialect
-----------

A document is an optional frontmatter block fenced by ``---`` lines, followed
by a body that is never interpreted::

    ---
    title: Replication basics
    tags: [multiplayer, networking]
    sources:
      - https://example.com/a#frag
    metadata:
      type: reference
      version: "1.0"
    ---
    body markdown, byte-for-byte

Supported inside the fences:

* ``key: value`` scalars. **Every scalar loads as a string** — there is no
  int/bool/date coercion, because half the callers store version numbers and
  dates that must survive verbatim.
* Quoting is **symmetric**: :func:`dumps` quotes a value that would otherwise
  be ambiguous and :func:`loads` unquotes it again, so ``loads(dumps(x)) == x``.
  A value counts as quoted only when it *starts* with a quote and the matching
  quote closes it; ``say "hi"`` is kept verbatim.
* ``key: [a, b]`` inline lists, comma-separated, quote-aware — ``[a, "b, c"]``
  is two items, not three.
* Block lists (``  - item``), items at any consistent indent.
* Nested maps (``metadata:`` + indented ``key: value`` lines). One level is
  what the memory-file convention needs; the parser recurses, so deeper nests
  work, but list-of-map is *not* supported (``- k: v`` is the scalar
  ``"k: v"``).
* ``[]`` is the empty list and ``{}`` the empty map; quoted (``"[]"``) they
  are strings.
* ``#`` opens a comment at the start of a line or after whitespace — so
  ``C# generics``, ``issue#12`` and ``https://example.com/a#frag`` survive.

Round-trip contract
-------------------

``parse(dump(meta, body)) == (meta, body)`` for any *meta* whose leaves are
strings, lists of strings, or nested maps of the same, and any *body*.
``dump`` re-emits fences even for empty *meta* when the body itself opens with
a ``---`` line, so a body starting with a horizontal rule is never swallowed
(``knowledge.md`` §3 bug 12).

Tolerance
---------

:func:`parse` is tolerant by default — a missing or malformed block yields
``({}, text)`` rather than raising, because scribe reads learnings on the
PreToolUse hot path and must not crash on a hand-edited ``.md``. Pass
``strict=True`` (researcher, captures, context rules) to get
:class:`FrontmatterError` instead.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "FENCE",
    "FrontmatterError",
    "dump",
    "dumps",
    "loads",
    "parse",
    "split",
]

FENCE = "---"

#: Indent used per nesting level when dumping.
_INDENT = "  "

#: Characters that force quoting when they *start* a scalar.
_LEADING_SPECIALS = (
    "-", "?", ":", "[", "]", "{", "}", "&", "*", "!", "|", ">", "'", '"',
    "%", "@", "`", "#",
)


class FrontmatterError(ValueError):
    """Raised when input falls outside the supported dialect.

    Subclasses ``ValueError`` so callers that already catch ``ValueError``
    around a frontmatter read — researcher and captures both do — keep working
    without naming this class.
    """

    def __init__(self, message: str, line: int = 0):
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line
        self.message = message


# --------------------------------------------------------------------------- #
# Scalar reading
# --------------------------------------------------------------------------- #

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
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
            if nxt in "nrt":
                out.append({"n": "\n", "r": "\r", "t": "\t"}[nxt])
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


def _scan_flow(text: str, start: int) -> tuple[int, list[int]]:
    """Scan an inline list from *start* (just past ``[``).

    Returns ``(close_index, comma_indexes)`` where ``close_index`` is the
    position of the matching ``]`` (or -1 when unbalanced) and the commas are
    the item separators found at depth zero.

    Two rules earn their keep against real files in the store:

    * A quote only opens a quoted span at the *start* of an item, the same rule
      :func:`_parse_scalar` uses. Without it the apostrophe in
      ``[What was done, What's pending]`` swallows the rest of the list — the
      exact shape ``scribe/default_templates/handoff.md`` ships.
    * Brackets nest. A glob's character class (``[ideas/*/0[0-9]-*.md, x]``,
      a real ``areas:`` value in the live store) must not close the list early.
    """
    commas: list[int] = []
    i = start
    depth = 1
    at_item_start = True
    while i < len(text):
        ch = text[i]
        if ch in " \t":
            i += 1
            continue
        if at_item_start and ch in ('"', "'"):
            end = _closing_quote(text[i:], ch)
            if end != -1:
                i += end + 1
                at_item_start = False
                continue
            # Unterminated: not really a quoted item, keep scanning verbatim.
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i, commas
        elif ch == "," and depth == 1:
            commas.append(i)
            at_item_start = True
            i += 1
            continue
        at_item_start = False
        i += 1
    return -1, commas


def _split_flow_items(inner: str, commas: list[int], offset: int) -> list[str]:
    """Cut *inner* at the depth-zero *commas* found by :func:`_scan_flow`."""
    items: list[str] = []
    start = 0
    for pos in commas:
        items.append(inner[start:pos - offset])
        start = pos - offset + 1
    items.append(inner[start:])
    return items


def _parse_value(text: str, line: int) -> Any:
    """Parse the right-hand side of ``key:`` — inline list, empty map, scalar."""
    if text.startswith("["):
        end, commas = _scan_flow(text, 1)
        if end != -1:
            rest = text[end + 1:].strip()
            if rest == "" or rest.startswith("#"):
                inner = text[1:end]
                if not inner.strip():
                    return []
                return [
                    _parse_scalar(raw)[0]
                    for raw in _split_flow_items(inner, commas, 1)
                    if raw.strip()
                ]
    if text.startswith("{"):
        stripped = _strip_comment(text)
        if stripped.replace(" ", "") == "{}":
            return {}
    return _parse_scalar(text)[0]


# --------------------------------------------------------------------------- #
# Document reading
# --------------------------------------------------------------------------- #

def _tokenize(text: str) -> list[tuple[int, int, str]]:
    """Return ``(line_number, indent_column, stripped_content)`` per real line.

    Blank lines and whole-line comments are dropped here so the parser below
    only ever sees content.
    """
    tokens: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        content = raw.strip()
        if not content or content.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        tokens.append((lineno, indent, content))
    return tokens


def _is_item(content: str) -> bool:
    return content.startswith("- ") or content == "-"


def _parse_list(
    tokens: list[tuple[int, int, str]], pos: int, indent: int
) -> tuple[list[str], int]:
    """Consume block-list items at column *indent* or deeper.

    Ragged indentation inside one list is accepted (the previous reader
    ignored indentation entirely, and live research entries predate any rule);
    what ends the list is a shallower line or a non-item line.
    """
    items: list[str] = []
    while pos < len(tokens):
        _lineno, col, content = tokens[pos]
        if col < indent or not _is_item(content):
            break
        items.append("" if content == "-" else _parse_scalar(content[2:])[0])
        pos += 1
    return items, pos


def _parse_map(
    tokens: list[tuple[int, int, str]], pos: int, indent: int
) -> tuple[dict[str, Any], int]:
    """Consume ``key: value`` lines sitting at column *indent*."""
    result: dict[str, Any] = {}
    while pos < len(tokens):
        lineno, col, content = tokens[pos]
        if col < indent:
            break
        if col > indent:
            raise FrontmatterError("unexpected indentation", lineno)
        if _is_item(content):
            raise FrontmatterError("list item without a parent key", lineno)

        key, sep, rest = content.partition(":")
        if not sep:
            raise FrontmatterError("expected 'key: value'", lineno)
        key = key.strip()
        if not key:
            raise FrontmatterError("empty key", lineno)
        pos += 1

        value_text = rest.strip()
        if value_text == "" or value_text.startswith("#"):
            # A bare key opens a block list or a nested map — whichever the
            # next line turns out to be. Nothing deeper following means an
            # empty list, which is what every caller's on-disk data means.
            child = tokens[pos] if pos < len(tokens) else None
            if child is None or child[1] < indent:
                result[key] = []
            elif _is_item(child[2]):
                # Items may sit at the key's own column (valid YAML) or deeper.
                result[key], pos = _parse_list(tokens, pos, child[1])
            elif child[1] > indent:
                result[key], pos = _parse_map(tokens, pos, child[1])
            else:
                result[key] = []
        else:
            result[key] = _parse_value(value_text, lineno)
    return result, pos


def loads(text: str) -> dict[str, Any]:
    """Parse a fence-free frontmatter document into a dict.

    Returns an empty dict for empty/whitespace-only input. Raises
    :class:`FrontmatterError` on anything outside the dialect.
    """
    tokens = _tokenize(text)
    if not tokens:
        return {}
    result, pos = _parse_map(tokens, 0, 0)
    if pos < len(tokens):  # pragma: no cover - _parse_map only stops on col < 0
        raise FrontmatterError("unparsed trailing content", tokens[pos][0])
    return result


def split(text: str) -> tuple[str, str] | None:
    """Split *text* into ``(frontmatter_text, body)`` without parsing either.

    Returns ``None`` when there is no complete ``---`` fenced block at the very
    top of the document. The body keeps its exact bytes, line endings included.
    """
    if not text.startswith(FENCE):
        return None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != FENCE:
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == FENCE:
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    return None


def parse(text: str, *, strict: bool = False) -> tuple[dict[str, Any], str]:
    """Split *text* into ``(meta, body)``.

    Tolerant by default: a document with no frontmatter, an unterminated fence,
    or a malformed block all yield ``({}, text)``. With ``strict=True`` each of
    those raises :class:`FrontmatterError` instead.
    """
    if not text:
        return {}, text
    parts = split(text)
    if parts is None:
        if strict:
            raise FrontmatterError(
                "missing opening or closing '---' frontmatter fence"
            )
        return {}, text
    fm_text, body = parts
    try:
        meta = loads(fm_text)
    except FrontmatterError:
        if strict:
            raise
        return {}, text
    return meta, body


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def _needs_quoting(value: str, *, in_flow: bool = False) -> bool:
    if value == "":
        return True
    if any(ch in value for ch in "\n\r\t"):
        # A raw newline would split the scalar across lines and re-parse the
        # tail as a new key; escape inside double quotes instead.
        return True
    if value != value.strip():
        # Leading/trailing whitespace is stripped on load, so it must be quoted
        # to survive the round trip.
        return True
    if value[0] in _LEADING_SPECIALS:
        return True
    if any(ch in value for ch in (":", "#")):
        # Conservative: colons and hashes inside values get quoted. ``loads``
        # would cope with most of them bare, but quoting is unambiguous and
        # symmetric — it is unquoted again on the way back in.
        return True
    if in_flow and any(ch in value for ch in (",", "[", "]")):
        # Inside ``[a, b]`` a comma or bracket would re-split the item.
        return True
    return False


def _dump_scalar(value: Any, *, in_flow: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _needs_quoting(text, in_flow=in_flow):
        escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        return f'"{escaped}"'
    return text


def _dump_into(
    data: dict[str, Any], level: int, lines: list[str], list_style: str
) -> None:
    pad = _INDENT * level
    for key, value in data.items():
        if isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}:")
                _dump_into(value, level + 1, lines, list_style)
        elif isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{pad}{key}: []")
            elif list_style == "inline":
                rendered = ", ".join(_dump_scalar(v, in_flow=True) for v in value)
                lines.append(f"{pad}{key}: [{rendered}]")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    lines.append(f"{pad}{_INDENT}- {_dump_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_dump_scalar(value)}")


def dumps(data: dict[str, Any], *, list_style: str = "block") -> str:
    """Emit a fence-free frontmatter document for *data*.

    Keys keep insertion order. ``list_style="block"`` renders lists as indented
    ``- item`` lines (researcher, captures, context rules); ``"inline"``
    renders ``[a, b]`` (scribe learnings and templates, whose on-disk files are
    already that shape).
    """
    if list_style not in ("block", "inline"):
        raise ValueError(f"list_style must be 'block' or 'inline', got {list_style!r}")
    lines: list[str] = []
    _dump_into(data, 0, lines, list_style)
    return "\n".join(lines) + "\n"


def _opens_with_fence(body: str) -> bool:
    first = body.split("\n", 1)[0]
    return first.rstrip() == FENCE


def dump(data: dict[str, Any], body: str = "", *, list_style: str = "block") -> str:
    """Render ``(meta, body)`` back into a document.

    Empty *meta* emits no fences at all — legacy files without frontmatter stay
    legacy-shaped — unless the body itself opens with a ``---`` line, in which
    case an empty block is written so :func:`parse` cannot mistake the body's
    horizontal rule for frontmatter.
    """
    # Mirror the body's line endings so a rewritten CRLF file is not mixed-EOL.
    nl = "\r\n" if "\r\n" in body else "\n"
    if not data:
        if not _opens_with_fence(body):
            return body
        return f"{FENCE}{nl}{FENCE}{nl}{body}"
    head = dumps(data, list_style=list_style)
    if nl != "\n":
        head = head.replace("\n", nl)
    return f"{FENCE}{nl}{head}{FENCE}{nl}{body}"
