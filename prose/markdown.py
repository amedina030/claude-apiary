"""Markdown block classifier for the prose linter.

Splits a document into blocks (paragraph, list item, heading, ...) with line
numbers, and strips inline markup from the prose so rules count the words and
punctuation a reader sees rather than syntax. This is the scoping layer: a
rule scoped to ``paragraph`` never sees a list item, a heading or a code
fence, which is what keeps structural em-dashes and semicolons (the joints in
a bullet or a table cell) out of the counts. The first measurement against
apiary's own notes flagged 70% of files, almost all on exactly that.

Not a CommonMark parser. A small line state machine covers what apiary's
prose uses: front matter, ATX and setext headings, fenced and indented code,
bullet and numbered lists with continuation lines, blockquotes, pipe tables,
HTML blocks and comments, thematic breaks and link reference definitions.
Everything else is a paragraph. Where CommonMark and a note-writer's habits
differ (a list marker interrupting a paragraph, a lone ``|`` row with no
delimiter line), the classifier sides with the habit, because the cost of a
misclassified block is a false finding and the cost of a permissive rule is
nothing.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import frontmatter

# ---------------------------------------------------------------------------
# Block kinds and scopes
# ---------------------------------------------------------------------------

FRONTMATTER = "frontmatter"
HEADING = "heading"
PARAGRAPH = "paragraph"
LIST = "list"
BLOCKQUOTE = "blockquote"
TABLE = "table"
CODE = "code"
HTML = "html"
THEMATIC = "thematic"
LINKDEF = "linkdef"

#: Kinds that carry prose a reader reads. Code, html, front matter and link
#: definitions never enter any scope but ``raw``.
PROSE_KINDS = (PARAGRAPH, LIST, HEADING, BLOCKQUOTE, TABLE)

#: Scope name -> the block kinds it selects. ``raw`` and ``sentence`` are
#: handled by :func:`segments` rather than by kind.
SCOPES: dict[str, tuple[str, ...]] = {
    "paragraph": (PARAGRAPH,),
    "list": (LIST,),
    "heading": (HEADING,),
    "blockquote": (BLOCKQUOTE,),
    "table": (TABLE,),
    "text": PROSE_KINDS,
    "sentence": PROSE_KINDS,
    "raw": (),
}


@dataclass
class Block:
    kind: str
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    raw: str  # the source lines, joined with "\n"
    prose: str = ""  # inline markup stripped, one entry per source line, "\n"-joined


@dataclass
class Segment:
    """A run of text a rule is applied to, with the line its first char is on."""

    line: int
    text: str
    kind: str = ""
    # ``text`` line breaks map back to source lines: ``line + text[:i].count("\n")``.
    _newline_lines: list[int] = field(default_factory=list, repr=False)

    def line_of(self, offset: int) -> int:
        return self.line + self.text[:offset].count("\n")


# ---------------------------------------------------------------------------
# Line patterns
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_THEMATIC_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_RE = re.compile(r"^(\s*)(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_HTML_OPEN_RE = re.compile(r"^ {0,3}<(?:!--|/?[A-Za-z][A-Za-z0-9-]*(?:[\s>]|$))")
_HTML_COMMENT_END = "-->"
_LINKDEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s+\S+")
_INDENTED_RE = re.compile(r"^(?: {4}|\t)")


def _is_blank(line: str) -> bool:
    return not line.strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t").replace("\t", "    "))


def _starts_block(line: str) -> bool:
    """True when *line* opens something other than a paragraph continuation."""
    return bool(
        _FENCE_RE.match(line)
        or _ATX_RE.match(line)
        or _THEMATIC_RE.match(line)
        or _LIST_RE.match(line)
        or _QUOTE_RE.match(line)
        or _HTML_OPEN_RE.match(line)
        or line.lstrip().startswith("|")
    )


# ---------------------------------------------------------------------------
# Inline stripping
# ---------------------------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"(`+)(?:.|\n)*?\1")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_AUTOLINK_RE = re.compile(r"<(?:https?|mailto):[^>]*>")
_BARE_URL_RE = re.compile(r"(?<![\w`])(?:https?://|www\.)\S+")
# Only real HTML tags are stripped. A placeholder such as ``<state-dir>`` or
# ``<role>`` in a template is a word the reader sees, not markup.
_HTML_TAGS = (
    "a|abbr|b|br|cite|code|del|details|dfn|div|em|hr|i|img|ins|kbd|mark|p|q|s|samp|small|"
    "span|strong|sub|summary|sup|time|u|var|wbr"
)
_INLINE_TAG_RE = re.compile(rf"</?(?:{_HTML_TAGS})(?:\s[^<>]*)?/?>", re.IGNORECASE)
_EMPHASIS_RE = re.compile(r"\*{1,3}|(?<!\w)_{1,3}(?=\w)|(?<=\w)_{1,3}(?!\w)|~~")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_ATX_STRIP_RE = re.compile(r"^ {0,3}#{1,6}[ \t]*|[ \t]+#+[ \t]*$")
_QUOTE_STRIP_RE = re.compile(r"^ {0,3}>[ \t]?")


def strip_inline(text: str) -> str:
    """Remove inline markup, keeping link and image text and the line count."""
    text = _CODE_SPAN_RE.sub(" ", text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _REF_LINK_RE.sub(r"\1", text)
    text = _AUTOLINK_RE.sub(" ", text)
    text = _BARE_URL_RE.sub(" ", text)
    text = _INLINE_TAG_RE.sub(" ", text)
    text = _EMPHASIS_RE.sub("", text)
    text = _ESCAPE_RE.sub(r"\1", text)
    return text


def _prose_for(kind: str, lines: list[str]) -> str:
    """Strip the block-level marker of *kind* from each line, then inline markup."""
    out: list[str] = []
    for i, line in enumerate(lines):
        if kind == HEADING:
            line = _ATX_STRIP_RE.sub("", line)
            if _SETEXT_RE.match(line):
                line = ""
        elif kind == LIST:
            m = _LIST_RE.match(line) if i == 0 else None
            line = line[m.end() :] if m else line.strip()
            line = re.sub(r"^\[[ xX]\][ \t]+", "", line)  # task-list checkbox
        elif kind == BLOCKQUOTE:
            while _QUOTE_STRIP_RE.match(line):
                line = _QUOTE_STRIP_RE.sub("", line, count=1)
            m = _LIST_RE.match(line)
            if m:
                line = line[m.end() :]
        elif kind == TABLE:
            if _TABLE_DELIM_RE.match(line):
                line = ""
            else:
                line = "  ".join(cell.strip() for cell in line.strip().strip("|").split("|"))
        out.append(line)
    return strip_inline("\n".join(out))


# ---------------------------------------------------------------------------
# Block classification
# ---------------------------------------------------------------------------


def blocks(text: str) -> list[Block]:
    """Classify *text* into blocks, in document order. Blank lines are dropped."""
    lines = text.split("\n")
    n = len(lines)
    out: list[Block] = []
    i = 0
    # After a list item, an indented non-marker line (even past a blank) still
    # belongs to the list; a flush-left line ends the context.
    in_list = False
    prev_blank = True

    def emit(kind: str, start: int, end: int) -> None:
        chunk = lines[start : end + 1]
        raw = "\n".join(chunk)
        prose = _prose_for(kind, chunk) if kind in PROSE_KINDS else ""
        out.append(Block(kind, start + 1, end + 1, raw, prose))

    # Front matter: ``core.frontmatter`` decides where the fenced block ends
    # (one dialect for the whole repo); only the line count is needed here.
    parts = frontmatter.split(text)
    if parts is not None:
        consumed = text[: len(text) - len(parts[1])]
        fm_lines = consumed.count("\n") + (0 if consumed.endswith("\n") else 1)
        emit(FRONTMATTER, 0, fm_lines - 1)
        i = fm_lines

    while i < n:
        line = lines[i]
        if _is_blank(line):
            prev_blank = True
            i += 1
            continue

        # Fenced code, to the matching fence or EOF.
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            j = i + 1
            while j < n:
                close = _FENCE_RE.match(lines[j])
                if (
                    close
                    and close.group(1)[0] == fence[0]
                    and len(close.group(1)) >= len(fence)
                    and not lines[j][close.end() :].strip()
                ):
                    break
                j += 1
            emit(CODE, i, min(j, n - 1))
            i = j + 1
            in_list = False
            prev_blank = False
            continue

        # HTML comment (possibly multi-line) or block-level tag to the next blank.
        if _HTML_OPEN_RE.match(line):
            j = i
            if line.lstrip().startswith("<!--"):
                while j < n and _HTML_COMMENT_END not in lines[j]:
                    j += 1
            else:
                while j + 1 < n and not _is_blank(lines[j + 1]):
                    j += 1
            emit(HTML, i, min(j, n - 1))
            i = j + 1
            in_list = False
            prev_blank = False
            continue

        if _THEMATIC_RE.match(line):
            emit(THEMATIC, i, i)
            i += 1
            in_list = False
            prev_blank = False
            continue

        if _ATX_RE.match(line):
            emit(HEADING, i, i)
            i += 1
            in_list = False
            prev_blank = False
            continue

        if _LINKDEF_RE.match(line):
            emit(LINKDEF, i, i)
            i += 1
            prev_blank = False
            continue

        # Indented code only outside a list and after a blank line; inside a
        # list the same indentation is a continuation.
        if prev_blank and not in_list and _INDENTED_RE.match(line):
            j = i
            while j + 1 < n and (_INDENTED_RE.match(lines[j + 1]) or _is_blank(lines[j + 1])):
                j += 1
            while j > i and _is_blank(lines[j]):
                j -= 1
            emit(CODE, i, j)
            i = j + 1
            prev_blank = False
            continue

        # Pipe table: a row followed by a delimiter row, or any row that
        # starts with ``|`` (generated tables always carry the delimiter, a
        # hand-typed fragment may not; either way it is not a paragraph).
        if ("|" in line and i + 1 < n and _TABLE_DELIM_RE.match(lines[i + 1])) or (
            line.lstrip().startswith("|")
        ):
            j = i
            while j + 1 < n and not _is_blank(lines[j + 1]) and "|" in lines[j + 1]:
                j += 1
            emit(TABLE, i, j)
            i = j + 1
            in_list = False
            prev_blank = False
            continue

        if _QUOTE_RE.match(line):
            j = i
            while j + 1 < n and not _is_blank(lines[j + 1]):
                nxt = lines[j + 1]
                if _QUOTE_RE.match(nxt) or not _starts_block(nxt):
                    j += 1
                else:
                    break
            emit(BLOCKQUOTE, i, j)
            i = j + 1
            in_list = False
            prev_blank = False
            continue

        if _LIST_RE.match(line):
            j = i
            while j + 1 < n and not _is_blank(lines[j + 1]):
                nxt = lines[j + 1]
                if _LIST_RE.match(nxt) or (_starts_block(nxt) and _indent(nxt) < 2):
                    break
                j += 1
            emit(LIST, i, j)
            i = j + 1
            in_list = True
            prev_blank = False
            continue

        # Indented continuation of an earlier list item, past a blank line.
        if in_list and _indent(line) >= 2:
            j = i
            while j + 1 < n and not _is_blank(lines[j + 1]) and not _LIST_RE.match(lines[j + 1]):
                j += 1
            emit(LIST, i, j)
            i = j + 1
            prev_blank = False
            continue

        # Paragraph: consecutive non-blank lines until something else opens.
        # A setext underline directly below turns it into a heading.
        j = i
        while j + 1 < n and not _is_blank(lines[j + 1]):
            # The underline check comes first: ``---`` directly under text is
            # a setext heading, and only after a blank line a thematic break.
            if _SETEXT_RE.match(lines[j + 1]):
                j += 1
                break
            if _starts_block(lines[j + 1]):
                break
            j += 1
        if _SETEXT_RE.match(lines[j]) and j > i:
            emit(HEADING, i, j)
        else:
            emit(PARAGRAPH, i, j)
        i = j + 1
        in_list = False
        prev_blank = False

    return _apply_directives(out)


# ``<!-- prose off -->`` ... ``<!-- prose on -->`` mutes every block between
# them: a document that has to quote a tell verbatim (this standard, a rule's
# own examples) keeps the text and drops it from every scope but ``raw``.
_DIRECTIVE_RE = re.compile(r"<!--\s*prose\s+(off|on)\s*-->", re.IGNORECASE)


def _apply_directives(out: list[Block]) -> list[Block]:
    muted = False
    for block in out:
        if block.kind == HTML:
            m = _DIRECTIVE_RE.search(block.raw)
            if m:
                muted = m.group(1).lower() == "off"
            continue
        if muted:
            block.prose = ""
    return out


# ---------------------------------------------------------------------------
# Scopes and segments
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")


def sentences(segment: Segment) -> list[Segment]:
    """Split one prose segment into sentences, each keeping its start line."""
    out: list[Segment] = []
    pos = 0
    text = segment.text
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        piece = text[pos : m.start()]
        if piece.strip():
            out.append(Segment(segment.line_of(pos), piece, segment.kind))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        out.append(Segment(segment.line_of(pos), tail, segment.kind))
    return out


def segments(text: str, scope: str, parsed: list[Block] | None = None) -> list[Segment]:
    """The runs of text a rule with *scope* is applied to, in document order.

    ``raw`` is the whole document as one segment. Every other scope yields one
    segment per selected block (its stripped prose), and ``sentence`` splits
    those further.
    """
    if scope == "raw":
        return [Segment(1, text, "raw")]
    kinds = SCOPES.get(scope)
    if kinds is None:
        raise ValueError(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")
    parsed = blocks(text) if parsed is None else parsed
    out = [
        Segment(b.start_line, b.prose, b.kind)
        for b in parsed
        if b.kind in kinds and b.prose.strip()
    ]
    if scope == "sentence":
        return [s for seg in out for s in sentences(seg)]
    return out


def word_count(text: str) -> int:
    return len(text.split())
