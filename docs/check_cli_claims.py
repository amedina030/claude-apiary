#!/usr/bin/env python3
"""CLI-claim reconciliation checker.

Sibling to docs/check.py. Where check.py validates doc *structure* (frontmatter,
coverage), this validates that the CLI claims in docs/reference/cli-tools.md still
match the tools' real argparse definitions — catching silent drift after refactors
(a documented flag that was renamed/removed, or a new subcommand nobody documented).

It reconciles NAMES in both directions per tool:
  - every documented subcommand/flag still exists in the tool's argparse, and
  - every real argparse subcommand/flag appears in the doc.

It deliberately does NOT touch descriptions or the hand-authored "Applies to"
grouping — the docs are the richer source for those, so this never rewrites them.
Report-only.

Mechanism: shell out to `python <tool> --help` (and `<tool> <sub> --help` per
subcommand) and parse the names out of the help text. Tools that can't be
introspected (libraries, GUI deps, console scripts) are reported as skipped —
never silently dropped.

Exit codes:
  0 — no drift (skips may be present)
  1 — drift found
  2 — usage / IO error (cli-tools.md missing)
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

DOCS_DIR = Path(__file__).parent
REPO_ROOT = DOCS_DIR.parent
CLI_DOC = DOCS_DIR / "reference" / "cli-tools.md"

HELP_TIMEOUT = 30  # seconds per --help subprocess

# Section headers in cli-tools.md that are not single introspectable argparse CLIs.
# Libraries, console scripts, GUI (heavy deps), redirect stubs, and prose categories.
SKIP_HEADERS = {
    "apiary",                       # console_script (core/cli.py) — different invocation
    "setup.py",                     # redirect stub, no live argparse
    "runner/cost_emit.py",          # library module
    "runner/config_loader.py",      # library module
    "gui/app.py",                   # requires the gui poetry group
    "gui/capture_session.py",       # requires the gui poetry group
    "gui/packaging/build.py",       # build script, not an argparse CLI
    "gui/packaging/make_icon.py",   # build script, not an argparse CLI
    "Test scripts",                 # prose category, not a tool
}

# Flags every argparse parser carries — never real drift.
ALWAYS_IGNORE_FLAGS = {"--help"}

IGNORE_MARKER = re.compile(r"<!--\s*cli-claims:\s*ignore:\s*(.*?)\s*-->", re.IGNORECASE)


class CannotIntrospect(Exception):
    """Raised when a tool's --help cannot be obtained."""


# --------------------------------------------------------------------------- #
# Doc parsing
# --------------------------------------------------------------------------- #

def parse_sections(text: str) -> list[tuple[str, str]]:
    """Split cli-tools.md into (header, body) pairs by top-level ## headers.

    Mirrors how docs/reference/cli_lookup.py splits the file, so the two stay
    in agreement about what a 'tool section' is.
    """
    sections: list[tuple[str, str]] = []
    header: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            if header is not None:
                sections.append((header, "\n".join(lines)))
            header = line[3:].strip()
            lines = []
        elif header is not None:
            lines.append(line)
    if header is not None:
        sections.append((header, "\n".join(lines)))
    return sections


def doc_subcommands(section: str) -> set[str]:
    """Extract documented subcommand names from a '### Subcommands' table.

    Reads the first column of the table that follows a '### Subcommands' heading,
    stripping backticks. Skips the literal '(none)' default row.
    """
    out: set[str] = set()
    lines = section.splitlines()
    in_table = False
    for i, line in enumerate(lines):
        if re.match(r"^###\s+Subcommands\b", line):
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("### ") or line.startswith("## "):
            break
        if not line.lstrip().startswith("|"):
            # blank line ends the table once we've started seeing rows
            if line.strip() == "" and out:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        # skip header row and the |---|---| separator row
        if first.lower() == "subcommand" or set(first) <= {"-", ":", " "}:
            continue
        name = first.strip("`").strip()
        if not name or name == "(none)":
            continue
        out.add(name)
    return out


def doc_flags(section: str) -> set[str]:
    """Extract documented long-flag names from the first column of Flag tables.

    Only reads rows of markdown tables whose first column header contains 'Flag'
    (e.g. 'Flag', 'Argument / Flag'). This deliberately ignores flags that appear
    in bash examples (`git rev-parse --show-toplevel`), in 'Usage' example columns,
    or in cross-reference prose (`report.py --by-request`) — those are not flag
    declarations and would be false positives.
    """
    flags: set[str] = set()
    in_flag_table = False
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_flag_table = False
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if set(first) <= {"-", ":", " "}:  # separator row — stay in table
            continue
        if "flag" in first.lower() and "--" not in first:  # header row
            in_flag_table = True
            continue
        if in_flag_table:
            flags.update(re.findall(r"--[A-Za-z][\w-]*", first))
    return flags - ALWAYS_IGNORE_FLAGS


def flag_mentions(section: str) -> set[str]:
    """Every long-flag token mentioned anywhere in a section (tables, usage, bash).

    Used only to SUPPRESS 'undocumented flag' findings: a flag shown in a usage
    example or bash block counts as documented even if it lacks a Flag-table row.
    Never used to create a finding, so noise (e.g. --show-toplevel) is harmless.
    """
    return set(re.findall(r"--[A-Za-z][\w-]*", section)) - ALWAYS_IGNORE_FLAGS


def doc_ignores(section: str) -> set[str]:
    """Tokens a maintainer marked as intentionally-not-reconciled for this tool.

    Syntax (anywhere in the tool's section):
        <!-- cli-claims: ignore: --legacy-flag, oldsubcommand -->
    """
    out: set[str] = set()
    for m in IGNORE_MARKER.finditer(section):
        for tok in m.group(1).replace(",", " ").split():
            out.add(tok.strip().strip("`"))
    return out


# --------------------------------------------------------------------------- #
# argparse introspection (via --help subprocess)
# --------------------------------------------------------------------------- #

def _run_help(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv + ["--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=HELP_TIMEOUT,
    )


def resolve_base(rel_path: str) -> tuple[list[str], str]:
    """Find a working invocation for a tool and return (base_argv, top_help_text).

    Tries `python <path>` first (matches most documented invocations); falls back
    to `python -m <dotted>` for package modules (e.g. runner.*) whose script-form
    import breaks. Raises CannotIntrospect if neither yields argparse help.
    """
    path = REPO_ROOT / rel_path
    if not path.exists():
        raise CannotIntrospect(f"{rel_path} does not exist")

    candidates = [[sys.executable, str(path)]]
    if rel_path.endswith(".py"):
        dotted = rel_path[:-3].replace("/", ".")
        candidates.append([sys.executable, "-m", dotted])

    last_err = "no usage output"
    for base in candidates:
        try:
            res = _run_help(base)
        except subprocess.TimeoutExpired:
            last_err = "--help timed out"
            continue
        text = res.stdout or ""
        if "usage:" in text:
            return base, text
        diag = (res.stderr or text).strip()
        if diag:
            last_err = diag.splitlines()[-1]
    raise CannotIntrospect(last_err)


def help_subcommands(help_text: str) -> set[str]:
    """Parse the {a,b,c} subcommand metavar from a parser's --help.

    Looks under 'positional arguments:' so a flag's choices ({todo,handoff}) under
    'options:' are never mistaken for subcommands.
    """
    m = re.search(r"positional arguments:\s*\n\s*\{([^}]*)\}", help_text)
    if not m:
        return set()
    return {s.strip() for s in m.group(1).split(",") if s.strip()}


def help_flags(help_text: str) -> set[str]:
    """Extract long flags from argparse --help option-declaration lines only.

    Reads indented lines that begin with '-' (the option entries), taking only the
    declaration segment before the description (a 2+ space gap). This avoids picking
    flag-looking tokens out of help descriptions (e.g. 'e.g. D--Professional-...')
    or wrapped usage lines.
    """
    flags: set[str] = set()
    for line in help_text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent < 2 or not stripped.startswith("-"):
            continue
        decl = re.split(r"\s{2,}", stripped, maxsplit=1)[0]
        flags.update(re.findall(r"--[A-Za-z][\w-]*", decl))
    return flags - ALWAYS_IGNORE_FLAGS


def introspect(rel_path: str) -> tuple[set[str], set[str]]:
    """Return (subcommands, flags) for a tool by reading its argparse --help."""
    base, top_help = resolve_base(rel_path)
    subs = help_subcommands(top_help)
    flags = help_flags(top_help)
    for sub in sorted(subs):
        try:
            res = _run_help(base + [sub])
        except subprocess.TimeoutExpired:
            continue
        if res.stdout and "usage:" in res.stdout:
            flags |= help_flags(res.stdout)
    return subs, flags


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

def reconcile(header: str, section: str) -> list[str]:
    """Return a list of drift findings for one tool section."""
    ignore = doc_ignores(section)
    c_subs, c_flags = introspect(header)

    findings: list[str] = []

    def stale(kind: str, doc_set: set[str], code_set: set[str]) -> None:
        # documented (authoritatively, in a table) but gone from argparse
        for name in sorted((doc_set - code_set) - ignore):
            findings.append(f"{header}: documented {kind} '{name}' not found in argparse")

    def undocumented(kind: str, code_set: set[str], mentioned: set[str]) -> None:
        # exists in argparse but appears nowhere in the doc section
        for name in sorted((code_set - mentioned) - ignore):
            findings.append(f"{header}: {kind} '{name}' exists in argparse but is undocumented")

    d_subs = doc_subcommands(section)
    stale("subcommand", d_subs, c_subs)
    undocumented("subcommand", c_subs, d_subs)

    stale("flag", doc_flags(section), c_flags)
    undocumented("flag", c_flags, flag_mentions(section))
    return findings


def is_tool_section(header: str) -> bool:
    """A section is an introspectable tool if it's a repo-relative .py path."""
    if header in SKIP_HEADERS:
        return False
    return header.endswith(".py") and "/" in header


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile cli-tools.md claims against real argparse")
    parser.add_argument("--only", metavar="HEADER",
                        help="Check a single tool section by its ## header (e.g. scribe/notes.py)")
    args = parser.parse_args()

    if not CLI_DOC.exists():
        print(f"ERROR: {CLI_DOC.relative_to(REPO_ROOT)} not found")
        sys.exit(2)

    sections = parse_sections(CLI_DOC.read_text(encoding="utf-8"))

    all_findings: list[str] = []
    skipped: list[str] = []
    checked = 0

    for header, section in sections:
        if args.only and header != args.only:
            continue
        if not is_tool_section(header):
            if header not in SKIP_HEADERS or args.only:
                skipped.append(f"{header} (not an introspectable argparse CLI)")
            continue
        try:
            all_findings.extend(reconcile(header, section))
            checked += 1
        except CannotIntrospect as e:
            skipped.append(f"{header} (could not introspect: {e})")

    if all_findings:
        print(f"cli-tools.md — {len(all_findings)} drift finding(s) across {checked} tool(s):\n")
        for f in all_findings:
            print(f"  DRIFT   {f}")
    else:
        print(f"cli-tools.md — {checked} tool(s) checked, all claims match argparse")

    if skipped:
        print(f"\nskipped {len(skipped)} section(s) (not reconciled):")
        for s in skipped:
            print(f"  SKIP    {s}")

    sys.exit(1 if all_findings else 0)


if __name__ == "__main__":
    main()
