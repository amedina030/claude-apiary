#!/usr/bin/env python3
"""Generate the CLI reference tables from each tool's real argparse.

Two documents are derived from ``check_cli_claims.inspect_tool()``:

* ``docs/reference/cli-index.md`` — the one-row-per-tool index that
  ``core/hooks/startup_prompt_hook.py`` injects into **every session**. It was
  the stalest doc in the repo (review X-5, ``last_verified: 2026-04-23``, still
  advertising a deleted installer); the table is now rebuilt from the parsers
  themselves and cannot name a tool that does not exist.
* ``docs/reference/cli-tools.md`` — the Subcommands / Flags / Arguments tables
  inside each tool section.

Only the regions between ``<!-- generated:start: … -->`` and
``<!-- generated:end: … -->`` are touched. Prose, usage examples, exit-code
notes and every other ``###`` subsection are hand-written and preserved.

**The reconciliation rule is `check_cli_claims`'s, applied instead of reported:**

* a table row naming a ``--flag`` (or, in a Subcommands table, a subcommand)
  that argparse no longer has is **deleted**;
* a real flag/subcommand that appears **nowhere** in that tool's section — not
  in a table, not in a usage example, not in prose — is **appended** to the
  section's primary table for its kind, seeded with the argparse help string;
* every other row, and every hand-written column (``Usage``, ``Applies to``,
  ``Required``, ``Description``), is left exactly as written.

So a deliberately curated table survives untouched — ``scribe/notes.py``'s
"Common flags" documents the flags worth knowing and shows the rest in its
usage examples — while a flag nobody documented becomes a diff the next commit
has to resolve. An intentional omission is marked the same way the checker
marks it: ``<!-- cli-claims: ignore: --some-flag -->`` in the section.

Sections that cannot be introspected (libraries, GUI-dependency scripts, the
hand-parsed dispatcher) are listed in ``check_cli_claims.SKIP_HEADERS`` and are
left alone; ``docs/test_generate_cli_docs.py`` asserts that cli-index.md's
hand-written "Not introspectable" table names exactly those.

Usage::

    python docs/generate_cli_docs.py            # --check (default); exit 1 on drift
    python docs/generate_cli_docs.py --write    # rewrite the generated blocks
    python docs/generate_cli_docs.py --check --diff
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
if str(DOCS_DIR) not in sys.path:
    sys.path.insert(0, str(DOCS_DIR))

import check_cli_claims as cc  # noqa: E402
import docgen  # noqa: E402

CLI_INDEX = DOCS_DIR / "reference" / "cli-index.md"
CLI_TOOLS = DOCS_DIR / "reference" / "cli-tools.md"

INDEX_KEY = "cli-index"
INDEX_HEADERS = ["Tool", "Purpose", "Subcommands / Key flags"]

#: Cap on how many names the index's third column spells out before eliding.
INDEX_MAX_NAMES = 12


# --------------------------------------------------------------------------- #
# Shared introspection
# --------------------------------------------------------------------------- #

def tool_headers(text: str) -> list[str]:
    """Section headers in cli-tools.md that this generator introspects."""
    return [h for h, _ in cc.parse_sections(text) if cc.is_tool_section(h)]


def interfaces(headers: list[str]) -> dict[str, cc.ToolInterface]:
    """Introspect every header in one parallel pass; drop what cannot be run."""
    results = cc.inspect_tools([cc.CONSOLE_SCRIPTS.get(h, h) for h in headers])
    out: dict[str, cc.ToolInterface] = {}
    for header in headers:
        result = results[cc.CONSOLE_SCRIPTS.get(header, header)]
        if isinstance(result, cc.ToolInterface):
            out[header] = result
    return out


def invocation(header: str, iface: cc.ToolInterface) -> str:
    """How a human types this tool.

    A console script (``apiary``) is typed by name — it has no file at its
    section header, which is exactly why ``CONSOLE_SCRIPTS`` exists.
    """
    if header in cc.CONSOLE_SCRIPTS:
        return header
    return iface.invocation()


def _load_interfaces() -> dict[str, cc.ToolInterface]:
    return interfaces(tool_headers(docgen.read_text(CLI_TOOLS)))


# --------------------------------------------------------------------------- #
# cli-index.md
# --------------------------------------------------------------------------- #

def _index_key(cell: str) -> str:
    """Normalise an index Tool cell so ``python -m runner.run``,
    ``python runner/run.py`` and ``runner/run.py`` all map to one key."""
    text = (cell or "").replace("`", "").strip()
    text = text.removeprefix("python ").strip()
    if text.startswith("-m "):
        text = text[3:].strip().replace(".", "/") + ".py"
    return text


def index_names(iface: cc.ToolInterface) -> str:
    """The third column: subcommands when there are any, else flags."""
    names = iface.subcommands or iface.positionals or iface.flags
    if not names:
        return "_(no arguments)_"
    shown = list(names[:INDEX_MAX_NAMES])
    more = len(names) - len(shown)
    text = ", ".join(shown)
    if more:
        text += f", … (+{more})"
    return docgen.escape_cell(text)


def build_index(ifaces: dict[str, cc.ToolInterface]):
    """Return the ``build`` callable for cli-index.md."""
    def build(text: str) -> str:
        old_body = docgen.block_body(text, INDEX_KEY)
        purposes: dict[str, str] = {}
        if old_body:
            found = docgen.find_tables(old_body.splitlines())
            if found:
                table = found[0][2]
                col = table.index_of("purpose")
                if col is not None:
                    for row in table.rows:
                        if len(row) > col:
                            purposes[_index_key(row[0])] = row[col]

        rows = []
        for header, iface in ifaces.items():
            invoke = invocation(header, iface)
            purpose = (purposes.get(_index_key(invoke))
                       or purposes.get(header)
                       or docgen.escape_cell(iface.description))
            rows.append([f"`{invoke}`", purpose, index_names(iface)])
        table = docgen.Table(headers=list(INDEX_HEADERS), rows=rows)
        return docgen.set_block(text, INDEX_KEY, docgen.render_table(table))
    return build


# --------------------------------------------------------------------------- #
# cli-tools.md
# --------------------------------------------------------------------------- #

def table_kind(table: docgen.Table) -> str | None:
    """Classify a table by its first column header.

    ``Argument / Flag`` counts as a flag table — its ``--flag`` rows are
    reconciled and its positional rows are left alone. A pure ``Argument``
    table holds positionals only.
    """
    first = (table.headers[0] if table.headers else "").lower()
    if "subcommand" in first:
        return "sub"
    if "flag" in first or "option" in first:
        return "flag"
    if "argument" in first:
        return "arg"
    return None


def section_span(lines: list[str], header: str) -> tuple[int, int]:
    """Line span of one ``## <header>`` section body (the header excluded)."""
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("## ") and ln[3:].strip() == header)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ") and not lines[i].startswith("### "):
            end = i
            break
    return start + 1, end


def reconcile_rows(table: docgen.Table, kind: str, live: set[str],
                   missing: list[str], seed: dict[str, str]) -> docgen.Table:
    """Drop rows code no longer has; append the names nothing documents.

    Only rows whose key is *recognisably* of this kind are ever dropped: a
    ``--flag`` in a flag table, a bare name in a Subcommands table. Anything
    else (a positional in an ``Argument / Flag`` table, the ``(none)`` default
    row, a prose row) is out of scope and survives.
    """
    rows: list[list[str]] = []
    for row in table.rows:
        key = docgen.cell_key(row[0]) if row else ""
        if kind == "flag":
            drop = key.startswith("--") and key not in live
        elif kind == "sub":
            drop = (bool(key) and key != "(none)" and not key.startswith("-")
                    and key not in live)
        else:
            drop = False
        if not drop:
            rows.append(list(row))

    width = len(table.headers)
    desc_col = table.index_of("description")
    desc_col = width - 1 if desc_col is None else desc_col
    for name in missing:
        cells = [""] * width
        cells[0] = f"`{name}`"
        cells[desc_col] = docgen.escape_cell(seed.get(name, ""))
        rows.append(cells)
    return docgen.Table(headers=list(table.headers), rows=rows, sep=table.sep)


def new_table(kind: str, names: list[str], seed: dict[str, str]) -> str:
    """A heading + table for a section that documents none of these names."""
    heading, col = {
        "sub": ("### Subcommands", "Subcommand"),
        "flag": ("### Flags", "Flag"),
        "arg": ("### Arguments", "Argument"),
    }[kind]
    rows = [[f"`{n}`", docgen.escape_cell(seed.get(n, ""))] for n in names]
    table = docgen.Table(headers=[col, "Description"], rows=rows)
    return heading + "\n\n" + docgen.render_table(table)


def documented_names(section: str, kind: str) -> set[str]:
    """What the section already says, by ``check_cli_claims``'s own rules."""
    if kind == "sub":
        return cc.doc_subcommands(section)
    if kind == "flag":
        return cc.flag_mentions(section)
    return set(re.findall(r"[A-Za-z][\w-]*", section))


def rebuild_section(text: str, header: str, iface: cc.ToolInterface) -> str:
    """Reconcile one tool section's tables, wrapping each in sentinels."""
    kinds = (
        ("sub", iface.subcommands, iface.sub_descs),
        ("flag", iface.flags, iface.flag_descs),
        ("arg", iface.positionals, iface.positional_descs),
    )
    for kind, names, seed in kinds:
        if not names:
            continue
        key = f"cli:{header}:{kind}"
        lines = text.splitlines()
        lo, hi = section_span(lines, header)
        section = "\n".join(lines[lo:hi])
        ignore = cc.doc_ignores(section)
        live = set(names)
        already = documented_names(section, kind)
        missing = [n for n in names if n not in already and n not in ignore]

        if docgen.block_span(text, key) is not None:
            body = docgen.block_body(text, key) or ""
            body_lines = body.splitlines()
            found = docgen.find_tables(body_lines)
            if not found:
                continue
            t_start, t_end, table = found[0]
            new = reconcile_rows(table, kind, live, missing, seed)
            rebuilt = "\n".join(body_lines[:t_start]
                                + docgen.render_table(new).splitlines()
                                + body_lines[t_end:])
            text = docgen.set_block(text, key, rebuilt)
            continue

        primary = None
        for t_start, t_end, table in docgen.find_tables(lines, lo, hi):
            if table_kind(table) == kind:
                primary = (t_start, t_end, table)
                break
        if primary is None:
            if not missing:
                continue
            insert = hi
            while insert > lo and not lines[insert - 1].strip():
                insert -= 1
            block = (f"{docgen.start_marker(key)}\n{new_table(kind, missing, seed)}\n"
                     f"{docgen.end_marker(key)}")
            text = "\n".join(lines[:insert] + ["", block] + lines[insert:])
            continue

        t_start, t_end, table = primary
        new = reconcile_rows(table, kind, live, missing, seed)
        block = (f"{docgen.start_marker(key)}\n{docgen.render_table(new)}\n"
                 f"{docgen.end_marker(key)}")
        text = "\n".join(lines[:t_start] + [block] + lines[t_end:])
    return text


def build_tools(ifaces: dict[str, cc.ToolInterface]):
    """Return the ``build`` callable for cli-tools.md."""
    def build(text: str) -> str:
        for header, iface in ifaces.items():
            text = rebuild_section(text, header, iface)
        return text if text.endswith("\n") else text + "\n"
    return build


def generators() -> list[docgen.Generator]:
    ifaces = _load_interfaces()
    return [
        docgen.Generator(CLI_INDEX, build_index(ifaces)),
        docgen.Generator(CLI_TOOLS, build_tools(ifaces)),
    ]


def main(argv: list[str] | None = None) -> int:
    return docgen.run_generators(
        generators,
        description="Generate the CLI reference tables from each tool's argparse",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
