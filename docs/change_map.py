#!/usr/bin/env python3
"""Fail a commit that changes mapped code without touching its document.

Review §5a-D.3: *a change to a mapped code file requires touching its
architecture doc in the same commit (or an explicit `docs: unchanged`
trailer); plus `last_verified` older than the file's last git change fails.*

Two checks over the staged changeset:

1. **Change mapping.** ``docs/change_map.json`` maps code globs to the docs
   that describe them. Staging a mapped code file without staging one of its
   docs is a finding.
2. **Staged-doc freshness.** A doc you are committing *is* being changed now,
   so its ``last_verified`` has to be today. ``docs/check.py`` compares against
   the file's last *commit*, which cannot see the change you are about to make
   — this is the half that can.

Both are waived by saying so out loud: a ``docs: unchanged`` trailer in the
commit message, or ``APIARY_DOCS_UNCHANGED=1`` in the environment.

**Where the trailer can be read.** At `pre-commit` time git has not written
this commit's message yet — ``.git/COMMIT_EDITMSG`` still holds the *previous*
commit's (verified empirically; the first commit in a fresh repo sees an empty
file, the second sees the first's message). So the trailer only works from a
``commit-msg`` hook, which is handed the real message file as ``$1`` and is
shipped as ``docs/hooks/commit-msg``. From ``pre-commit`` the environment
variable is the escape. Passing ``--message`` with a stale file is worse than
passing nothing, so ``--staged`` alone never guesses at a message path.

Usage::

    python docs/change_map.py --staged                       # pre-commit
    python docs/change_map.py --staged --message "$1"        # commit-msg
    python docs/change_map.py --paths a/b.py c/d.md          # ad hoc
    python docs/change_map.py --list

Exit codes:
  0 — nothing to report (or the check was waived)
  1 — a mapped code file changed without its doc, or a staged doc is stale
  2 — the map is missing or malformed
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent
MAP_PATH = DOCS_DIR / "change_map.json"

TRAILER_RE = re.compile(r"^\s*docs:\s*unchanged\s*$", re.IGNORECASE | re.MULTILINE)
ENV_ESCAPE = "APIARY_DOCS_UNCHANGED"
FRONTMATTER_DATE_RE = re.compile(r"^last_verified:\s*\"?([0-9]{4}-[0-9]{2}-[0-9]{2})\"?",
                                 re.MULTILINE)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def load_map(path: Path = MAP_PATH) -> list[dict]:
    """Read change_map.json. Raises ValueError on anything malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{path} could not be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"{path} has no 'entries' list")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("code") or not entry.get("docs"):
            raise ValueError(f"{path}: every entry needs 'code' and 'docs' globs")
    return entries


def staged_paths() -> list[str]:
    """Repo-relative paths in the staged changeset (added/copied/modified/renamed)."""
    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        return []
    return [ln.strip().replace("\\", "/") for ln in res.stdout.splitlines() if ln.strip()]


def read_message(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def waived(message: str) -> bool:
    """True when the author has said the docs are deliberately unchanged."""
    if os.environ.get(ENV_ESCAPE, "").strip() not in ("", "0", "false", "no"):
        return True
    return bool(TRAILER_RE.search(message))


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def matches(path: str, patterns) -> bool:
    """fnmatch against repo-relative paths, with `**` crossing directories."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if "**" in pattern and fnmatch.fnmatch(path, pattern.replace("**/", "*")):
            return True
    return False


def mapping_findings(paths: list[str], entries: list[dict]) -> list[str]:
    """One finding per entry whose code changed and whose docs did not."""
    findings = []
    for entry in entries:
        touched = [p for p in paths if matches(p, entry["code"])]
        if not touched:
            continue
        if any(matches(p, entry["docs"]) for p in paths):
            continue
        shown = ", ".join(touched[:3]) + (f" (+{len(touched) - 3})" if len(touched) > 3 else "")
        findings.append(
            f"{entry.get('id', '?')}: {shown} changed, but none of "
            f"{', '.join(entry['docs'])} did.\n"
            f"            {entry.get('why', '')}".rstrip())
    return findings


def stale_doc_findings(paths: list[str], today: str | None = None) -> list[str]:
    """Staged docs whose `last_verified` is not the date of this commit."""
    today = today or date.today().isoformat()
    findings = []
    for rel in paths:
        if not rel.startswith("docs/") or not rel.endswith(".md"):
            continue
        if rel.startswith("docs/review/"):     # dated snapshots, deleted at close-out
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        m = FRONTMATTER_DATE_RE.search(path.read_text(encoding="utf-8"))
        if not m:                              # no frontmatter: docs/check.py's problem
            continue
        if m.group(1) < today:
            findings.append(
                f"{rel}: last_verified is {m.group(1)}, but you are changing the "
                f"doc today ({today}).\n"
                f"            Re-read it against the code and bump the date.")
    return findings


# --------------------------------------------------------------------------- #

def report(findings: list[str], *, from_commit_msg: bool) -> None:
    print(f"change map — {len(findings)} finding(s):\n")
    for f in findings:
        print(f"  BLOCKED {f}")
    escape = ("add a `docs: unchanged` trailer to the commit message"
              if from_commit_msg else f"set {ENV_ESCAPE}=1 for this commit")
    print(f"\nUpdate the doc in the same commit, or {escape} if this change "
          f"genuinely does not affect what it claims.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail a commit that changes mapped code without its doc")
    parser.add_argument("--staged", action="store_true",
                        help="Check the staged changeset (pre-commit mode)")
    parser.add_argument("--paths", nargs="*", metavar="PATH",
                        help="Check an explicit list of paths instead of the staged set")
    parser.add_argument("--message", metavar="FILE",
                        help="Read the commit message from FILE when looking for a "
                             "`docs: unchanged` trailer (commit-msg hook: pass $1)")
    parser.add_argument("--list", action="store_true",
                        help="Print the mapping and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        entries = load_map()
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.list:
        for entry in entries:
            print(f"{entry.get('id', '?')}")
            print(f"  code: {', '.join(entry['code'])}")
            print(f"  docs: {', '.join(entry['docs'])}")
            if entry.get("why"):
                print(f"  why : {entry['why']}")
        return 0

    if not args.staged and args.paths is None:
        parser.error("pass --staged, --paths, or --list")

    paths = list(args.paths) if args.paths is not None else staged_paths()
    if not paths:
        return 0

    if waived(read_message(args.message)):
        return 0

    findings = mapping_findings(paths, entries) + stale_doc_findings(paths)
    if not findings:
        return 0
    report(findings, from_commit_msg=bool(args.message))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
