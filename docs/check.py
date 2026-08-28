#!/usr/bin/env python3
"""Documentation framework conformance checker.

Four checks, and deliberately no more (review §5 Phase 5). What this used to
do was validate the *shape* of six frontmatter keys and then assert that the
strings "budgeter", "scribe" and "core" appeared somewhere in the concatenated
reference docs — which is how it stayed green for four months while a migration
invalidated most of the content it was checking.

1. **Frontmatter is present and well-formed** — parsed by ``core.frontmatter``,
   the one dialect; six required keys; a `type` and `scope` from the known sets.
2. **`last_verified` is not older than the file's last git change.** This is the
   check with teeth. Editing a doc without re-verifying it now fails the commit,
   which is the only mechanism that makes "verified on this date" mean anything
   (review §5a-D.3). Skipped for untracked files and shallow clones, where the
   git dates are not real.
3. **The index is complete and honest** — every doc has an `_index.md` entry, and
   every entry points at a doc that exists.
4. **Coverage, against a tool list derived from the tree** — every hook script,
   command file and argparse entry point is named in its reference doc.
   ``KNOWN_TOOLS`` used to be a hard-coded ``{"budgeter", "scribe", "core"}``,
   so harden, refiner, compass, researcher, runner, incubator, captures, gui
   and scripts were all invisible to it (T-2026-287). The list is now whatever
   the repo actually contains.

Errors are broken docs (1-3); coverage gaps are warnings, because a missing
row is a smaller lie than a wrong one and the generators fix most of them.

Exit codes:
  0 — no errors (warnings may be present)
  1 — errors found (or --strict and any issues found)
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DOCS_DIR = Path(__file__).parent
REPO_ROOT = DOCS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
from core import frontmatter  # noqa: E402

FRAMEWORK_FILE = DOCS_DIR / "_framework.md"
INDEX_FILE = DOCS_DIR / "_index.md"
SKIP_FILES = {"_framework.md", "_index.md"}
SKIP_DIRS = {"commands", "hooks", "reports"}  # Not framework docs — command defs,
#                          hook scripts, and post-mortem reports (own schema:
#                          docs/standards/report-style.md, not this checker's types)

VALID_TYPES = {"reference", "architecture", "standard", "guide"}
REQUIRED_FIELDS = {"type", "title", "scope", "description", "framework_version", "last_verified"}

#: Scopes that are not a tool directory.
EXTRA_SCOPES = {"project", "docs"}

#: Top-level directories that hold data or config, never code to document.
NON_TOOL_DIRS = {"profiles", "context-rules", "cron_registry", "captures"}

#: Doc subtrees that are dated snapshots of a tree that has since changed on
#: purpose. They still need frontmatter and an index entry, but "re-verify it
#: against the code" is meaningless for a document whose value is that it
#: records what was true on one day. `docs/review/` is deleted at close-out
#: (T-2026-271); the snapshot line at the top of each file says so.
SNAPSHOT_DIRS = {"review"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- #
# The tool list, derived from the tree
# --------------------------------------------------------------------------- #

def known_tools() -> set[str]:
    """Top-level tool directories, read off the repo rather than hard-coded.

    A tool directory is a non-hidden top-level directory that ships Python or
    slash commands. ``captures/`` names both a tool and a GUI data directory,
    so the data-only names are excluded by name.
    """
    tools = set()
    for entry in sorted(REPO_ROOT.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in NON_TOOL_DIRS and not any(entry.glob("*.py")):
            continue
        if any(entry.glob("*.py")) or (entry / "commands").is_dir():
            tools.add(entry.name)
    return tools


def valid_scopes() -> set[str]:
    return known_tools() | EXTRA_SCOPES


# --------------------------------------------------------------------------- #
# Git dates
# --------------------------------------------------------------------------- #

def _git(*args: str) -> str:
    """Run a read-only git command in the repo. Empty string on any failure."""
    try:
        res = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                             text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def is_shallow() -> bool:
    """True for a shallow clone, where per-file history does not exist.

    ``actions/checkout`` defaults to depth 1, which would make every file's
    "last change" the tip commit — and every doc stale. CI asks for the full
    history; anywhere else, the check simply does not run.
    """
    return _git("rev-parse", "--is-shallow-repository").strip() == "true"


def last_change_dates() -> dict[str, str]:
    """``{repo-relative path: YYYY-MM-DD}`` of each file's newest commit.

    One ``git log`` for the whole history rather than one per doc — 28 docs
    meant 28 subprocesses on the hook path, and this runs on every commit.
    """
    out = _git("log", "--format=@%as", "--name-only", "--no-renames")  # author date: survives cherry-pick/rebase
    dates: dict[str, str] = {}
    current = ""
    for line in out.splitlines():
        if line.startswith("@"):
            current = line[1:].strip()
            continue
        path = line.strip()
        if path and current and path not in dates:  # log is newest-first
            dates[path] = current
    return dates


# --------------------------------------------------------------------------- #
# Docs
# --------------------------------------------------------------------------- #

def parse_frontmatter(path: Path) -> dict | None:
    """Extract frontmatter from a markdown doc as a dict, or None if absent.

    Uses ``core.frontmatter`` — the one dialect (Phase 3.3) — in tolerant mode,
    so a doc whose block is malformed reports as "missing frontmatter" the way
    a doc with no block at all always has, rather than crashing the checker.
    """
    fm, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
    return fm or None


def get_framework_version() -> str:
    """Read the current framework version from _framework.md."""
    fm = parse_frontmatter(FRAMEWORK_FILE)
    if not fm or "version" not in fm:
        print("ERROR: _framework.md missing version in frontmatter")
        sys.exit(1)
    return fm["version"]


def find_docs() -> list[Path]:
    """Find all markdown files under docs/ that should be checked."""
    docs = []
    for root, _dirs, files in os.walk(DOCS_DIR):
        for f in files:
            if not f.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, f), DOCS_DIR)
            rel_posix = rel.replace("\\", "/")
            if rel_posix in SKIP_FILES:
                continue
            if rel_posix.split("/")[0] in SKIP_DIRS:
                continue
            docs.append(Path(root) / f)
    return sorted(docs)


def check_doc(path: Path, framework_version: str, scopes: set[str],
              git_dates: dict[str, str]) -> tuple[list[str], list[str]]:
    """Check a single doc for conformance. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    rel = path.relative_to(DOCS_DIR).as_posix()

    fm = parse_frontmatter(path)
    if fm is None:
        return [f"{rel}: missing frontmatter block"], warnings

    missing = REQUIRED_FIELDS - set(fm.keys())
    if missing:
        errors.append(f"{rel}: missing required fields: {', '.join(sorted(missing))}")

    doc_type = fm.get("type", "")
    if doc_type and doc_type not in VALID_TYPES:
        errors.append(f"{rel}: invalid type '{doc_type}' (expected: {', '.join(sorted(VALID_TYPES))})")

    scope = fm.get("scope", "")
    if scope and scope not in scopes:
        errors.append(f"{rel}: invalid scope '{scope}' (expected: {', '.join(sorted(scopes))})")

    is_snapshot = bool(SNAPSHOT_DIRS & set(rel.split("/")[:-1]))
    lv = str(fm.get("last_verified", "")).strip('"')
    if lv and not DATE_RE.match(lv):
        errors.append(f"{rel}: last_verified '{lv}' not in YYYY-MM-DD format")
    elif lv and not is_snapshot:
        changed = git_dates.get(path.relative_to(REPO_ROOT).as_posix())
        if changed and lv < changed:
            errors.append(
                f"{rel}: last_verified {lv} is older than the file's last change "
                f"({changed}) — re-verify the doc against the code and bump it, "
                f"(merge with a merge commit, not a squash — a squash re-dates every doc)")

    doc_version = fm.get("framework_version", "")
    if doc_version and doc_version != framework_version:
        warnings.append(f"{rel}: framework_version '{doc_version}' behind current '{framework_version}'")

    return errors, warnings


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #

def _read_ref_doc(name: str) -> str:
    path = DOCS_DIR / "reference" / name
    return path.read_text(encoding="utf-8").lower() if path.exists() else ""


def check_hooks_coverage(tools: set[str]) -> list[str]:
    """Every hook script under any tool's hooks/ is named in hooks.md."""
    issues = []
    content = _read_ref_doc("hooks.md")
    if not content:
        return ["coverage: docs/reference/hooks.md not found"]
    for tool in sorted(tools):
        hooks_dir = REPO_ROOT / tool / "hooks"
        if not hooks_dir.is_dir():
            continue
        for py in sorted(hooks_dir.glob("*.py")):
            if py.name.startswith("test_") or py.name == "__init__.py":
                continue
            if py.stem not in content:
                issues.append(f"coverage: hook {tool}/hooks/{py.name} not in hooks.md")
    return issues


def check_commands_coverage(tools: set[str]) -> list[str]:
    """Every <tool>/commands/*.md is named in slash-commands.md."""
    issues = []
    content = _read_ref_doc("slash-commands.md")
    if not content:
        return ["coverage: docs/reference/slash-commands.md not found"]
    for tool in sorted(tools):
        cmd_dir = REPO_ROOT / tool / "commands"
        if not cmd_dir.is_dir():
            continue
        for md in sorted(cmd_dir.glob("*.md")):
            if md.stem not in content:
                issues.append(f"coverage: command {tool}/commands/{md.name} not in slash-commands.md")
    return issues


def check_cli_coverage(tools: set[str]) -> list[str]:
    """Every argparse entry point has a `## <path>` section in cli-tools.md.

    Matched on the section header, not on a substring of the whole document:
    "check" appears in cli-tools.md a hundred times, which is how
    ``docs/check.py`` counted as documented while having no section at all.
    """
    issues = []
    path = DOCS_DIR / "reference" / "cli-tools.md"
    if not path.exists():
        return ["coverage: docs/reference/cli-tools.md not found"]
    headers = {line[3:].strip()
               for line in path.read_text(encoding="utf-8").splitlines()
               if line.startswith("## ") and not line.startswith("### ")}
    # A console script's section is named for the command, not the module it
    # runs (`## apiary`, not `## core/cli.py`). One mapping, in the checker
    # that already owns it.
    sys.path.insert(0, str(DOCS_DIR))
    try:
        from check_cli_claims import CONSOLE_SCRIPTS
        headers |= {module for module in CONSOLE_SCRIPTS.values()
                    if set(CONSOLE_SCRIPTS) & headers}
    except ImportError:
        pass
    for tool in sorted(tools):
        tool_path = REPO_ROOT / tool
        for py in sorted(tool_path.glob("*.py")):
            if py.name.startswith("test_") or py.name == "__init__.py":
                continue
            try:
                content = py.read_text(encoding="utf-8")
            except OSError:
                continue
            if "argparse" not in content or "__main__" not in content:
                continue
            rel = py.relative_to(REPO_ROOT).as_posix()
            if rel not in headers:
                issues.append(f"coverage: CLI tool {rel} has no section in cli-tools.md")
    return issues


def check_index(docs: list[Path]) -> list[str]:
    """The index lists every doc, and every doc it lists exists."""
    if not INDEX_FILE.exists():
        return ["index: docs/_index.md not found"]
    text = INDEX_FILE.read_text(encoding="utf-8")
    lowered = text.lower()
    issues = []
    for doc in docs:
        rel = doc.relative_to(DOCS_DIR).as_posix()
        if rel.lower() not in lowered:
            issues.append(f"index: {rel} not listed in _index.md")
    for target in re.findall(r"\]\(([^)#]+\.md)\)", text):
        if not (DOCS_DIR / target).exists():
            issues.append(f"index: _index.md links to {target}, which does not exist")
    return issues


# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Documentation framework conformance checker")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on any issue (warnings included)")
    args = parser.parse_args()

    framework_version = get_framework_version()
    docs = find_docs()
    if not docs:
        print(f"Framework v{framework_version} — no docs to check")
        return

    tools = known_tools()
    scopes = valid_scopes()
    git_dates = {} if is_shallow() else last_change_dates()

    all_errors: list[str] = []
    all_warnings: list[str] = []
    for doc in docs:
        errors, warnings = check_doc(doc, framework_version, scopes, git_dates)
        all_errors.extend(errors)
        all_warnings.extend(warnings)

    all_errors.extend(check_index(docs))
    all_warnings.extend(check_hooks_coverage(tools))
    all_warnings.extend(check_commands_coverage(tools))
    all_warnings.extend(check_cli_coverage(tools))

    if all_errors or all_warnings:
        total = len(all_errors) + len(all_warnings)
        print(f"Framework v{framework_version} — {total} issue(s):\n")
        for issue in all_errors:
            print(f"  ERROR   {issue}")
        for issue in all_warnings:
            print(f"  WARN    {issue}")
        if all_errors:
            sys.exit(1)
        if args.strict and all_warnings:
            sys.exit(1)
    else:
        print(f"Framework v{framework_version} — {len(docs)} doc(s) across "
              f"{len(tools)} tool(s), all conformant")


if __name__ == "__main__":
    main()
