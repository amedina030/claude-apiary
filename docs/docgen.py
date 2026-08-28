#!/usr/bin/env python3
"""Shared machinery for the ``docs/generate_*.py`` family.

Review §5a-D.1: *a doc that can drift is generated from code or tested against
it*. The generators built on this module all work the same way:

* A doc carries one or more **generated blocks**, delimited by
  ``<!-- generated:start: <key> -->`` / ``<!-- generated:end: <key> -->``.
  Everything outside a block is hand-written and never touched.
* A generator is a ``(path, build)`` pair where ``build(text) -> text`` returns
  what the file *should* contain. ``--check`` diffs that against the file and
  exits 1 on drift; ``--write`` writes it.

**The generator owns the row set; the doc owns the cell content.** A table's
row *names* (subcommands, flags, hook names, config keys) come from code, so a
renamed flag or a new hook fails ``--check``. The *descriptions* are carried
over from the existing row, because argparse help strings are terse and the
hand-written ones are the reason the reference docs are worth reading. A row
that has no existing description is seeded from code (argparse help) and left
for a human to improve.

That split is deliberate: it makes the checks catch exactly the drift class
that rotted the docs before (names that no longer exist, code that nothing
documents) without turning the reference into `--help` dumps.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent

START_RE_TMPL = r"<!--\s*generated:start:\s*{key}\s*-->"
END_RE_TMPL = r"<!--\s*generated:end:\s*{key}\s*-->"


def start_marker(key: str) -> str:
    return f"<!-- generated:start: {key} -->"


def end_marker(key: str) -> str:
    return f"<!-- generated:end: {key} -->"


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #

def block_span(text: str, key: str) -> tuple[int, int] | None:
    """Character span of the whole block for *key*, sentinels included."""
    s = re.search(START_RE_TMPL.format(key=re.escape(key)), text)
    if s is None:
        return None
    e = re.search(END_RE_TMPL.format(key=re.escape(key)), text[s.end():])
    if e is None:
        return None
    return s.start(), s.end() + e.end()


def block_body(text: str, key: str) -> str | None:
    """The body between the sentinels for *key*, or None when absent."""
    span = block_span(text, key)
    if span is None:
        return None
    inner = text[span[0]:span[1]]
    inner = re.sub(r"^" + START_RE_TMPL.format(key=re.escape(key)) + r"\n?", "", inner)
    inner = re.sub(r"\n?" + END_RE_TMPL.format(key=re.escape(key)) + r"$", "", inner)
    return inner


def set_block(text: str, key: str, body: str) -> str:
    """Replace the body of an existing block. Raises when the block is absent."""
    span = block_span(text, key)
    if span is None:
        raise KeyError(f"no generated block '{key}' in this document")
    wrapped = f"{start_marker(key)}\n{body.rstrip()}\n{end_marker(key)}"
    return text[:span[0]] + wrapped + text[span[1]:]


def wrap_region(text: str, key: str, start: int, end: int, body: str) -> str:
    """Wrap ``text[start:end]`` in sentinels for *key*, replacing it with *body*.

    Used once, when a doc first grows a generated block; afterwards
    :func:`set_block` finds it by key.
    """
    wrapped = f"{start_marker(key)}\n{body.rstrip()}\n{end_marker(key)}"
    return text[:start] + wrapped + text[end:]


def upsert_block(text: str, key: str, body: str,
                 fallback: Callable[[str], tuple[int, int]] | None = None) -> str:
    """:func:`set_block` when the block exists, else wrap the fallback region."""
    if block_span(text, key) is not None:
        return set_block(text, key, body)
    if fallback is None:
        raise KeyError(f"no generated block '{key}' and no fallback region")
    start, end = fallback(text)
    return wrap_region(text, key, start, end, body)


# --------------------------------------------------------------------------- #
# Markdown tables
# --------------------------------------------------------------------------- #

_SEP_CELL = re.compile(r"^:?-{2,}:?$")


@dataclass
class Table:
    """One markdown pipe table: header cells, separator, and body rows."""

    headers: list[str]
    rows: list[list[str]] = field(default_factory=list)
    sep: list[str] | None = None

    def index_of(self, header_fragment: str) -> int | None:
        """Index of the first header whose text contains *header_fragment*."""
        frag = header_fragment.lower()
        for i, h in enumerate(self.headers):
            if frag in h.lower():
                return i
        return None

    def row_map(self, key_col: int = 0) -> dict[str, list[str]]:
        """Rows keyed by the normalised token in *key_col*."""
        return {cell_key(r[key_col]): r for r in self.rows if len(r) > key_col}


def split_row(line: str) -> list[str]:
    """Split one ``| a | b |`` line into cells, honouring escaped pipes."""
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    cells, buf, i = [], [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("\\|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def is_separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(_SEP_CELL.match(c.strip() or "-") for c in cells)


def find_tables(lines: Sequence[str], start: int = 0, stop: int | None = None
                ) -> list[tuple[int, int, Table]]:
    """Locate every pipe table in ``lines[start:stop]``.

    Returns ``(first_line, last_line_exclusive, Table)`` triples. A table is a
    header row, a separator row, and the contiguous rows after it.
    """
    stop = len(lines) if stop is None else stop
    out: list[tuple[int, int, Table]] = []
    i = start
    while i < stop - 1:
        line, nxt = lines[i], lines[i + 1]
        if not line.lstrip().startswith("|") or not nxt.lstrip().startswith("|"):
            i += 1
            continue
        header = split_row(line)
        sep = split_row(nxt)
        if not is_separator(sep):
            i += 1
            continue
        rows: list[list[str]] = []
        j = i + 2
        while j < stop and lines[j].lstrip().startswith("|"):
            rows.append(split_row(lines[j]))
            j += 1
        out.append((i, j, Table(headers=header, rows=rows, sep=sep)))
        i = j
    return out


def render_table(table: Table) -> str:
    """Render a :class:`Table` back to markdown (no column padding)."""
    sep = table.sep or ["-" * max(3, len(h)) for h in table.headers]
    lines = ["| " + " | ".join(table.headers) + " |",
             "|" + "|".join(sep) + "|"]
    width = len(table.headers)
    for row in table.rows:
        cells = list(row[:width]) + [""] * max(0, width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def escape_cell(text: str) -> str:
    """Make *text* safe inside a markdown table cell."""
    return (text or "").replace("|", r"\|").replace("\n", " ").strip()


def cell_key(cell: str) -> str:
    """Normalise a first-column cell to the bare name it documents.

    ``` `--only HEADER` ``` → ``--only``; ``` `add` ``` → ``add``. Keeps the
    merge stable when a doc spells a flag with its metavar and argparse does
    not (or vice versa).
    """
    text = (cell or "").replace("`", " ").replace("\\|", " ").strip()
    text = text.split("|")[0].strip()          # `-o` / `--only` alias cells
    token = text.split()[0] if text.split() else ""
    return token.strip().rstrip(",")


def merge_table(old: Table | None, names: Sequence[str],
                headers: Sequence[str], seed: dict[str, str] | None = None,
                desc_col: int | None = None) -> Table:
    """Reconcile *old* against the authoritative *names* from code.

    Rows keep their hand-written cells and their existing order; rows whose
    name is gone from code are dropped; names with no row are appended, with
    *seed* (argparse help) in the description column.
    """
    seed = seed or {}
    headers = list(headers) if old is None else list(old.headers)
    width = len(headers)
    if desc_col is None:
        desc_col = width - 1
    existing = old.row_map() if old is not None else {}
    wanted = [str(n) for n in names]
    wanted_set = set(wanted)

    rows: list[list[str]] = []
    used: set[str] = set()
    if old is not None:
        for row in old.rows:
            key = cell_key(row[0]) if row else ""
            if key in wanted_set and key not in used:
                cells = list(row[:width]) + [""] * max(0, width - len(row))
                rows.append(cells)
                used.add(key)
    for name in wanted:
        if name in used:
            continue
        cells = [""] * width
        cells[0] = f"`{name}`"
        if 0 <= desc_col < width:
            cells[desc_col] = escape_cell(seed.get(name, ""))
        rows.append(cells)
        used.add(name)
    _ = existing  # kept for readability of the merge contract
    return Table(headers=headers, rows=rows, sep=old.sep if old else None)


@dataclass
class Record:
    """One authoritative row: a stable key plus the cells code knows about."""

    key: str
    cells: dict[str, str] = field(default_factory=dict)


def sync_table(old: Table | None, records: Sequence[Record],
               headers: Sequence[str], generated: Iterable[str],
               key_of_row: Callable[[list[str], list[str]], str] | None = None,
               ) -> Table:
    """Rebuild a table from *records*, keeping the hand-written columns.

    Rows follow *records* in order, so the table's shape is code's to decide.
    Columns named in *generated* are overwritten from the record (a config
    default, a hook's matcher — facts, not prose); every other column is
    carried over from the row with the same key, or seeded from the record
    when it has a value and the doc does not.
    """
    headers = list(headers)
    generated = set(generated)
    key_of_row = key_of_row or (lambda row, _h: cell_key(row[0]) if row else "")
    old_by_key: dict[str, list[str]] = {}
    old_headers = list(old.headers) if old else []
    if old is not None:
        for row in old.rows:
            old_by_key.setdefault(key_of_row(row, old_headers), row)

    rows: list[list[str]] = []
    for rec in records:
        prev = old_by_key.get(rec.key)
        cells: list[str] = []
        for h in headers:
            if h in generated:
                cells.append(escape_cell(rec.cells.get(h, "")))
                continue
            carried = ""
            if prev is not None and h in old_headers:
                idx = old_headers.index(h)
                carried = prev[idx] if idx < len(prev) else ""
            cells.append(carried or escape_cell(rec.cells.get(h, "")))
        rows.append(cells)
    return Table(headers=headers, rows=rows)


def first_table(body: str) -> Table | None:
    """The first pipe table in *body*, or None."""
    found = find_tables(body.splitlines())
    return found[0][2] if found else None


# --------------------------------------------------------------------------- #
# Generator harness
# --------------------------------------------------------------------------- #

@dataclass
class Generator:
    """One generated document: where it lives and how to rebuild it."""

    path: Path
    build: Callable[[str], str]
    label: str = ""

    def rel(self) -> str:
        try:
            return self.path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return str(self.path)


def _diff(rel: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=2,
    ))


def run_generators(generators: "Iterable[Generator] | Callable[[], Iterable[Generator]]",
                   *, description: str,
                   argv: Sequence[str] | None = None) -> int:
    """``--check`` / ``--write`` entry point shared by every generator script.

    *generators* may be a callable, and for anything expensive it **must** be:
    the list is built only after argparse has handled ``--help``. Building it
    eagerly made ``generate_cli_docs.py --help`` do a full introspection pass —
    which introspects ``generate_cli_docs.py``, which runs ``--help``, which…
    (the recursion showed up as "could not introspect: --help timed out").

    Exit codes: 0 in sync (or written), 1 drift found under ``--check``,
    2 a source doc is missing.
    """
    parser = argparse.ArgumentParser(description=description)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", default=True,
                      help="Report drift and exit 1 (default)")
    mode.add_argument("--write", action="store_true",
                      help="Rewrite the generated blocks in place")
    parser.add_argument("--diff", action="store_true",
                        help="Print a unified diff for each drifted file")
    args = parser.parse_args(list(argv) if argv is not None else None)

    drifted: list[str] = []
    written: list[str] = []
    checked = 0

    for gen in (generators() if callable(generators) else generators):
        rel = gen.rel()
        if not gen.path.is_file():
            print(f"ERROR: {rel} not found")
            return 2
        current = gen.path.read_text(encoding="utf-8")
        try:
            rebuilt = gen.build(current)
        except KeyError as exc:
            print(f"ERROR: {rel}: {exc}")
            return 2
        checked += 1
        if rebuilt == current:
            continue
        if args.write:
            gen.path.write_text(rebuilt, encoding="utf-8", newline="\n")
            written.append(rel)
        else:
            drifted.append(rel)
            if args.diff:
                print(_diff(rel, current, rebuilt))

    if args.write:
        if written:
            print(f"rewrote {len(written)} of {checked} generated doc(s):")
            for rel in written:
                print(f"  WROTE   {rel}")
        else:
            print(f"{checked} generated doc(s) already in sync")
        return 0

    if drifted:
        print(f"{len(drifted)} of {checked} generated doc(s) are out of date:")
        for rel in drifted:
            print(f"  DRIFT   {rel}")
        print("\nRun the same command with --write to regenerate.")
        return 1
    print(f"{checked} generated doc(s) in sync")
    return 0


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - library module
    print(__doc__)
    sys.exit(0)
