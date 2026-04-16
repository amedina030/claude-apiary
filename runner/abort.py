"""Abort handler for crashed runner runs (T-2026-128).

Archives crashed artifacts to runner/crashes/<uuid>/, removes worktree
and branch, deletes the lockfile, emits a rollback summary, and saves
a scribe blocker note.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from . import run_lock

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CRASHES_DIR = SCRIPT_DIR / "crashes"

ARTIFACT_DIRS = ("specs", "plans", "executions", "hardens", "reports")


def _archive_artifacts(uuid: str) -> Path:
    """Copy any existing stage artifacts into runner/crashes/<uuid>/."""
    dest = CRASHES_DIR / uuid
    dest.mkdir(parents=True, exist_ok=True)
    for dirname in ARTIFACT_DIRS:
        src = SCRIPT_DIR / dirname / f"{uuid}.json"
        if src.exists():
            shutil.copy2(src, dest / f"{dirname}_{uuid}.json")
    lock_data = run_lock.read(uuid)
    if lock_data:
        (dest / "lockfile.json").write_text(
            json.dumps(lock_data, indent=2), encoding="utf-8",
        )
    return dest


def _remove_worktree(worktree_path: str) -> bool:
    """Attempt to remove a git worktree. Returns True on success or if
    already gone."""
    if not worktree_path:
        return True
    wt = Path(worktree_path)
    if not wt.exists():
        return True
    r = subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _delete_branches(uuid: str) -> list[str]:
    """Delete runner branches matching this UUID. Returns list of deleted
    branch names."""
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/runner/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []

    from .run import _find_runner_branches_from_refs
    refs = result.stdout.splitlines()
    branches = _find_runner_branches_from_refs(refs, uuid)

    deleted = []
    for branch in branches:
        d = subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True, text=True,
        )
        if d.returncode == 0:
            deleted.append(branch)
    return deleted


def _build_summary(uuid: str, lock_data: dict | None, archive_path: Path,
                   branches_deleted: list[str],
                   worktree_removed: bool) -> str:
    """Build a human-readable rollback summary."""
    lines = [f"=== Abort summary for run {uuid} ==="]
    if lock_data:
        stage = lock_data.get("stage", "unknown")
        step = lock_data.get("step_number", "?")
        pid = lock_data.get("pid", "?")
        started = lock_data.get("started_at", 0)
        age_min = (time.time() - started) / 60 if started else 0
        wt = lock_data.get("worktree_path", "")
        lines.append(f"Crashed at: stage={stage}, step={step}, PID={pid}")
        lines.append(f"Age: {age_min:.0f} minutes")
        if wt:
            lines.append(f"Worktree: {wt} ({'removed' if worktree_removed else 'removal failed'})")
    else:
        lines.append("Lockfile was corrupt or missing metadata.")

    lines.append(f"Artifacts archived to: {archive_path}")
    if branches_deleted:
        lines.append(f"Branches deleted: {', '.join(branches_deleted)}")
    else:
        lines.append("No runner branches found for this UUID.")
    lines.append("Lockfile deleted.")
    return "\n".join(lines)


def _save_blocker_note(summary: str, session_id: str = "") -> None:
    """Save a scribe blocker note with the rollback summary."""
    launch = Path.home() / ".claude" / "apiary_launch.py"
    if not launch.exists():
        return
    cmd = [
        sys.executable, str(launch), "scribe/notes.py", "add",
        "--type", "blocker",
        "--content", summary,
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
    subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def abort_run(uuid: str, session_id: str = "") -> str:
    """Full abort sequence for a single crashed run. Returns the summary."""
    lock_data = run_lock.read(uuid)

    if lock_data and not run_lock.is_stale(lock_data):
        pid = lock_data.get("pid", "?")
        raise RuntimeError(
            f"Run {uuid} is still active (PID {pid}) — kill the process first."
        )

    archive_path = _archive_artifacts(uuid)

    worktree_path = (lock_data or {}).get("worktree_path", "")
    wt_ok = _remove_worktree(worktree_path)

    branches = _delete_branches(uuid)

    run_lock.delete(uuid)

    summary = _build_summary(uuid, lock_data, archive_path, branches, wt_ok)
    print(summary)

    _save_blocker_note(summary, session_id)
    return summary


def abort_all(session_id: str = "") -> int:
    """Abort all stale runs. Returns count of runs aborted."""
    stale = run_lock.scan_stale()
    if not stale:
        print("No stale runs found.")
        return 0
    count = 0
    for entry in stale:
        uuid = entry.get("uuid", "")
        if not uuid:
            continue
        try:
            abort_run(uuid, session_id)
            count += 1
        except RuntimeError as e:
            print(f"Skipping {uuid}: {e}", file=sys.stderr)
    return count
