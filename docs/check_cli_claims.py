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
introspected (libraries, GUI deps) are reported as skipped — never silently
dropped. Console scripts have no file at their section header's name, so they
are mapped to the module behind the entry point (see CONSOLE_SCRIPTS).

Exit codes:
  0 — no drift (skips may be present)
  1 — drift found
  2 — usage / IO error (cli-tools.md missing)
"""

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

DOCS_DIR = Path(__file__).parent
REPO_ROOT = DOCS_DIR.parent
CLI_DOC = DOCS_DIR / "reference" / "cli-tools.md"

HELP_TIMEOUT = 30  # seconds per --help subprocess

# Section headers in cli-tools.md that are not single introspectable argparse CLIs.
# Libraries, console scripts, GUI (heavy deps), and prose categories.
SKIP_HEADERS = {
    "runner/cost_emit.py",          # library module
    "runner/config_loader.py",      # library module
    "gui/app.py",                   # requires the gui poetry group
    "gui/capture_session.py",       # requires the gui poetry group
    "gui/packaging/build.py",       # build script, not an argparse CLI
    "gui/packaging/make_icon.py",   # build script, not an argparse CLI
    "core/hooks/dispatch.py",       # hook entry point: one positional verb,
                                    # hand-parsed — argparse would cost an
                                    # import on the hottest path in the toolkit
    "docs/docgen.py",               # library shared by the generators
    "docs/test_doc_examples.py",    # pytest module, not a CLI
    "scripts/install_repo_hooks.py",  # no argparse: --help would INSTALL
    "Test scripts",                 # prose category, not a tool
}

# Console scripts declared in pyproject's [project.scripts]. Their section
# header is the command name, so there is no `<repo>/<header>` file to run
# `--help` against — map the header to the module whose `main()` the entry
# point calls and introspect that instead. Reconciling these matters more than
# most: they are the CLIs a user types by name.
CONSOLE_SCRIPTS = {
    "apiary": "core/cli.py",        # pyproject.toml: apiary = "core.cli:main"
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

#: Concurrent ``--help`` subprocesses, repo-wide. Introspecting ~48 tools plus
#: their ~120 subcommands is ~250 process spawns; serially on Windows that is
#: minutes, and it runs in ``docs/hooks/pre-commit``. The work is entirely
#: subprocess wait time, so threads (which release the GIL around
#: ``subprocess.run``) are the right tool — but the pools nest (tools × their
#: subcommands), so one semaphore caps the real fan-out rather than each pool
#: capping its own.
HELP_WORKERS = 8
_HELP_SLOTS = threading.BoundedSemaphore(HELP_WORKERS)

#: In-process memo, so one run never shells out twice for the same argv.
_HELP_MEMO: dict[str, dict] = {}
#: Loaded from / flushed to the on-disk cache; see :func:`_cache_path`.
_HELP_CACHE: dict[str, dict] | None = None
_HELP_CACHE_DIRTY = False

# Directories that can never contribute to a tool's --help output. Skipped when
# fingerprinting the tree so the cache check stays a few tens of milliseconds.
_FINGERPRINT_SKIP = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".repos", ".apiary",
    ".claude", ".pytest_cache", "dist", "build", ".mypy_cache", ".ruff_cache",
}


def _cache_path() -> Path:
    """Where the shared ``--help`` cache lives (OS temp, never the repo)."""
    return Path(tempfile.gettempdir()) / "apiary-cli-help-cache.json"


def _repo_fingerprint() -> str:
    """A token that changes whenever anything a ``--help`` depends on changes.

    Every ``.py``/``.json`` under the checkout, by newest mtime and count, plus
    the interpreter. Deliberately coarse: one edit anywhere invalidates the
    whole cache, which is the only invalidation rule that cannot go stale (a
    tool's help text can come from any module it imports). The win it buys is
    the second checker in the same commit — ``docs/check_cli_claims.py`` and
    ``docs/generate_cli_docs.py --check`` both run in ``docs/hooks/pre-commit``
    and would otherwise shell out to ~44 tools twice.
    """
    newest = 0.0
    count = 0
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _FINGERPRINT_SKIP]
        for name in files:
            if not (name.endswith(".py") or name.endswith(".json")):
                continue
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                continue
            count += 1
    return f"{sys.version_info[:3]}|{count}|{newest:.6f}"


def _load_cache() -> dict[str, dict]:
    """Read the on-disk cache, or start an empty one on any mismatch/error."""
    global _HELP_CACHE
    if _HELP_CACHE is not None:
        return _HELP_CACHE
    _HELP_CACHE = {}
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("fingerprint") == _repo_fingerprint():
            entries = data.get("entries")
            if isinstance(entries, dict):
                _HELP_CACHE = entries
    except (OSError, ValueError, TypeError):
        _HELP_CACHE = {}
    return _HELP_CACHE


def _flush_cache() -> None:
    """Best-effort write of the cache at interpreter exit. Never raises."""
    if not _HELP_CACHE_DIRTY or not _HELP_CACHE:
        return
    try:
        payload = {"fingerprint": _repo_fingerprint(), "entries": _HELP_CACHE}
        path = _cache_path()
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError):
        pass


atexit.register(_flush_cache)


def _cache_key(argv: list[str]) -> str:
    """Interpreter-independent key for one ``--help`` invocation."""
    return json.dumps(["python"] + list(argv[1:]))


def _run_help(argv: list[str]) -> subprocess.CompletedProcess:
    """``<argv> --help``, memoised in-process and across processes.

    Set ``APIARY_DOCS_NO_HELP_CACHE=1`` to force real subprocesses (the cache
    tests do).
    """
    global _HELP_CACHE_DIRTY
    full = argv + ["--help"]
    if os.environ.get("APIARY_DOCS_NO_HELP_CACHE") == "1":
        return subprocess.run(full, cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=HELP_TIMEOUT)

    key = _cache_key(full)
    hit = _HELP_MEMO.get(key) or _load_cache().get(key)
    if isinstance(hit, dict) and "stdout" in hit:
        _HELP_MEMO[key] = hit
        return subprocess.CompletedProcess(
            full, hit.get("returncode", 0), hit.get("stdout", ""), hit.get("stderr", ""))

    with _HELP_SLOTS:
        res = subprocess.run(full, cwd=REPO_ROOT, capture_output=True,
                             text=True, timeout=HELP_TIMEOUT)
    entry = {"returncode": res.returncode, "stdout": res.stdout or "",
             "stderr": res.stderr or ""}
    _HELP_MEMO[key] = entry
    _load_cache()[key] = entry
    _HELP_CACHE_DIRTY = True
    return res


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


def _entries(help_text: str, section: str) -> list[tuple[str, str]]:
    """Parse one argparse help section into ``(declaration, description)`` pairs.

    argparse lays every entry out as ``  <decl>  <description>`` with a 2+ space
    gap and wraps long descriptions onto deeper-indented continuation lines.
    Subparser choices are nested one level deeper under the ``{a,b,c}`` metavar,
    so this returns them too — which is where subcommand descriptions come from.
    """
    lines = help_text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip().rstrip(":").lower() == section)
    except StopIteration:
        return []
    out: list[tuple[str, str]] = []
    decl_indent: int | None = None
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:                      # next section header
            break
        stripped = line.strip()
        parts = re.split(r"\s{2,}", stripped, maxsplit=1)
        is_continuation = (
            out and decl_indent is not None and indent > decl_indent and len(parts) == 1
        )
        if is_continuation:
            name, desc = out[-1]
            out[-1] = (name, (desc + " " + stripped).strip())
            continue
        decl_indent = indent
        out.append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    return out


def help_description(help_text: str) -> str:
    """The parser's own ``description=`` — the prose between usage and the
    first argument section. One line, whitespace collapsed."""
    body = re.split(r"\n(?:positional arguments|options|optional arguments):", help_text, maxsplit=1)[0]
    body = re.sub(r"^usage:.*?(?=\n\S|\n\n|\Z)", "", body, flags=re.S)
    return " ".join(body.split())


def help_subcommand_descs(help_text: str) -> dict[str, str]:
    """``{subcommand: help}`` from the nested subparser entries."""
    out: dict[str, str] = {}
    for decl, desc in _entries(help_text, "positional arguments"):
        if decl.startswith("{") or decl.startswith("-"):
            continue
        name = decl.split(",")[0].strip()
        if re.fullmatch(r"[A-Za-z][\w-]*", name):
            out[name] = desc
    return out


def help_positionals(help_text: str) -> list[tuple[str, str]]:
    """``[(name, help)]`` for real positional arguments.

    Excludes the ``{a,b,c}`` subcommand metavar and the subparser choices
    nested under it — those are subcommands, reported separately.
    """
    entries = _entries(help_text, "positional arguments")
    if any(d.startswith("{") for d, _ in entries):
        # A parser with subcommands: everything after the metavar is a choice.
        return []
    return [(d.split()[0], desc) for d, desc in entries
            if d and not d.startswith("-")]


def help_flag_descs(help_text: str) -> dict[str, str]:
    """``{--flag: help}`` for every long flag declared in an options section."""
    out: dict[str, str] = {}
    for section in ("options", "optional arguments"):
        for decl, desc in _entries(help_text, section):
            if not decl.startswith("-"):
                continue
            for flag in re.findall(r"--[A-Za-z][\w-]*", decl):
                if flag in ALWAYS_IGNORE_FLAGS:
                    continue
                out.setdefault(flag, desc)
    return out


@dataclass
class ToolInterface:
    """Everything ``docs/generate_cli_docs.py`` needs about one tool's argparse."""

    rel_path: str
    base_argv: list[str]
    description: str = ""
    subcommands: list[str] = field(default_factory=list)
    sub_descs: dict[str, str] = field(default_factory=dict)
    positionals: list[str] = field(default_factory=list)
    positional_descs: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    flag_descs: dict[str, str] = field(default_factory=dict)

    def invocation(self) -> str:
        """How a human types this tool, per the argv that actually worked.

        ``python -m runner.run`` for package modules whose script form breaks,
        ``python scribe/notes.py`` for everything else.
        """
        rest = self.base_argv[1:]
        if rest[:2] and rest[0] == "-m":
            return f"python -m {rest[1]}"
        return f"python {self.rel_path}"


def inspect_tool(rel_path: str) -> ToolInterface:
    """Full argparse introspection for one tool, names *and* help strings."""
    base, top = resolve_base(rel_path)
    subs = sorted(help_subcommands(top))
    iface = ToolInterface(
        rel_path=rel_path,
        base_argv=list(base),
        description=help_description(top),
        subcommands=subs,
        sub_descs={k: v for k, v in help_subcommand_descs(top).items() if k in subs},
        positionals=[n for n, _ in help_positionals(top)],
        positional_descs=dict(help_positionals(top)),
        flag_descs=help_flag_descs(top),
    )
    flags = help_flags(top)
    for sub, res in _sub_helps(base, subs):
        if res is None or not res.stdout or "usage:" not in res.stdout:
            continue
        flags |= help_flags(res.stdout)
        for flag, desc in help_flag_descs(res.stdout).items():
            iface.flag_descs.setdefault(flag, desc)
    iface.flags = sorted(flags)
    return iface


def _sub_helps(base: list[str], subs: Sequence[str]):
    """``(sub, CompletedProcess|None)`` for each subcommand, run concurrently.

    ``scribe/notes.py`` alone has 21 subcommands; serially that is 21 process
    spawns for one tool. A timeout yields ``None`` rather than raising, so one
    wedged subcommand cannot lose the whole tool.
    """
    if not subs:
        return []
    def one(sub: str):
        try:
            return sub, _run_help(base + [sub])
        except subprocess.TimeoutExpired:
            return sub, None
    with ThreadPoolExecutor(max_workers=min(HELP_WORKERS, len(subs))) as pool:
        return list(pool.map(one, subs))


def introspect(rel_path: str) -> tuple[set[str], set[str]]:
    """Return (subcommands, flags) for a tool by reading its argparse --help."""
    iface = inspect_tool(rel_path)
    return set(iface.subcommands), set(iface.flags)


def inspect_tools(rel_paths: Sequence[str]) -> dict[str, ToolInterface | CannotIntrospect]:
    """Introspect several tools at once. Failures come back as the exception.

    Order of the returned mapping follows *rel_paths* so reports stay stable.
    """
    paths = list(rel_paths)
    results: dict[str, ToolInterface | CannotIntrospect] = {}
    if not paths:
        return results
    with ThreadPoolExecutor(max_workers=min(HELP_WORKERS, len(paths))) as pool:
        futures = {pool.submit(inspect_tool, p): p for p in paths}
        done: dict[str, ToolInterface | CannotIntrospect] = {}
        for future in as_completed(futures):
            rel = futures[future]
            try:
                done[rel] = future.result()
            except CannotIntrospect as exc:
                done[rel] = exc
    for rel in paths:
        results[rel] = done[rel]
    return results


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

def reconcile(header: str, section: str,
              iface: "ToolInterface | None" = None) -> list[str]:
    """Return a list of drift findings for one tool section.

    *iface* lets a caller that already introspected in parallel pass the result
    in; without it this introspects the tool itself.
    """
    ignore = doc_ignores(section)
    if iface is None:
        iface = inspect_tool(CONSOLE_SCRIPTS.get(header, header))
    c_subs, c_flags = set(iface.subcommands), set(iface.flags)

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
    """A section is introspectable if it's a repo-relative .py path or a
    console script we know how to resolve to one."""
    if header in SKIP_HEADERS:
        return False
    if header in CONSOLE_SCRIPTS:
        return True
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

    selected: list[tuple[str, str]] = []
    for header, section in sections:
        if args.only and header != args.only:
            continue
        if not is_tool_section(header):
            if header not in SKIP_HEADERS or args.only:
                skipped.append(f"{header} (not an introspectable argparse CLI)")
            continue
        selected.append((header, section))

    # One parallel introspection pass for every selected tool, then a serial
    # reconcile over the results — so the ~250 `--help` spawns overlap.
    ifaces = inspect_tools([CONSOLE_SCRIPTS.get(h, h) for h, _ in selected])
    for header, section in selected:
        result = ifaces[CONSOLE_SCRIPTS.get(header, header)]
        if isinstance(result, CannotIntrospect):
            skipped.append(f"{header} (could not introspect: {result})")
            continue
        all_findings.extend(reconcile(header, section, result))
        checked += 1

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
