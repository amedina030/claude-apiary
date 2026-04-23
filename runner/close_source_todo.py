#!/usr/bin/env python3
"""
Close the scribe TODO linked to a runner branch that was just merged.

Invoked from the post-merge git hook (runner/hooks/post-merge) after a
merge commit lands on master. Walks the merge's second-parent commits
for any ``runner/<uuid> step N: ...`` subject, extracts the uuid, looks
up runner/run_history.jsonl for the last entry with that uuid, and — if
its ``source`` field names a scribe note — calls ``scribe/notes.py done``
to close it.

Failure is silent by design: a hook that errors on a merged commit
disrupts the operator's workflow for no recoverable reason. The runner
branch is already merged; the TODO can be closed manually if this
script can't figure out what to do.

Stdlib only, per docs/standards/code-style.md.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from runner.target_repo import run_history_path

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_HISTORY_FILE = run_history_path()
NOTES_SCRIPT = SCRIPT_DIR.parent / "scribe" / "notes.py"

# Scribe note id pattern: T-YYYY-N+, H-YYYY-N+, etc. We only auto-close
# ``T-`` (todos) — closing handoffs/decisions/contexts would be weird.
_TODO_ID_RE = re.compile(r"^T-\d{4}-\d+$")

_RUNNER_SUBJECT_RE = re.compile(
    r"^runner/(?P<uuid>[A-Za-z0-9][\w-]*) step \d+:"
)


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(SCRIPT_DIR.parent),
    )


def uuids_from_merge(merge_sha: str) -> list[str]:
    """Return every runner uuid found in commit subjects reachable from
    the merge's second parent but not from the first.

    For a plain ``git merge runner/<branch>`` that's all the commits the
    runner made on the branch being merged in.
    """
    parents_res = _git("rev-list", "--parents", "-n", "1", merge_sha)
    if parents_res.returncode != 0:
        return []
    parts = parents_res.stdout.strip().split()
    if len(parts) < 3:
        return []  # not a merge commit (only one parent)
    first_parent, second_parent = parts[1], parts[2]
    rev_res = _git("rev-list", f"^{first_parent}", second_parent)
    if rev_res.returncode != 0:
        return []
    uuids = []
    seen = set()
    for sha in rev_res.stdout.splitlines():
        sha = sha.strip()
        if not sha:
            continue
        subj_res = _git("log", "--format=%s", "-n", "1", sha)
        if subj_res.returncode != 0:
            continue
        m = _RUNNER_SUBJECT_RE.match(subj_res.stdout.strip())
        if m:
            u = m.group("uuid")
            if u not in seen:
                seen.add(u)
                uuids.append(u)
    return uuids


def source_for_uuid(uuid: str) -> str:
    """Scan run_history.jsonl (newest last) for the most recent entry with
    this uuid and return its ``source`` field. Empty string if no entry or
    no source set.
    """
    if not RUN_HISTORY_FILE.exists():
        return ""
    source = ""
    try:
        for line in RUN_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                entry = json.loads(s)
            except json.JSONDecodeError:
                continue
            if entry.get("uuid") == uuid:
                src = entry.get("source", "")
                if isinstance(src, str) and src.strip():
                    source = src.strip()
    except OSError:
        return ""
    return source


def close_todo(todo_id: str) -> bool:
    """Call ``scribe/notes.py done <todo_id>``. Returns True on success.

    Idempotent on the scribe side: closing an already-done note is safe.
    """
    if not NOTES_SCRIPT.exists():
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(NOTES_SCRIPT), "done", todo_id],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main():
    # Hook invocation: `git merge` calls post-merge with $1 = is_squash (0/1),
    # but the hook gives us no direct access to the merged branch name. We
    # key on HEAD's parentage instead — works for every merge shape (merge
    # commit, squash, ff-with-no-ff-flag).
    merge_sha_res = _git("rev-parse", "HEAD")
    if merge_sha_res.returncode != 0:
        return 0
    merge_sha = merge_sha_res.stdout.strip()
    uuids = uuids_from_merge(merge_sha)
    if not uuids:
        return 0
    for uuid in uuids:
        source = source_for_uuid(uuid)
        if not source or not _TODO_ID_RE.match(source):
            continue
        if close_todo(source):
            print(f"[post-merge] Closed {source} (runner {uuid[:8]} merged).",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
