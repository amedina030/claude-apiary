"""Abort handler for crashed runner runs (T-2026-128).

Archives crashed artifacts to ``<state>/runner/crashes/<uuid>/``, removes the
worktree and branch, deletes the lockfile, and emits a rollback summary.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import run_lock
from .git_lib import git
from .target_repo import artifacts_root, resolve_target_repo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Where the stage artifacts actually live. This read `runner/<dir>/<uuid>.json`
# in the source tree until now — the pre-migration location, stranded since
# e887b17 — so every abort archived nothing at all (review runner Bug 11 /
# T-2026-278a). Same for the crash archive itself: runtime state belongs in
# the state dir, not in a gitignored corner of the checkout.
ARTIFACTS_ROOT = artifacts_root()
CRASHES_DIR = ARTIFACTS_ROOT / "crashes"

ARTIFACT_DIRS = ("specs", "plans", "executions", "hardens", "reports")


def _archive_artifacts(uuid: str) -> Path:
    """Copy any existing stage artifacts into ``<state>/runner/crashes/<uuid>/``."""
    dest = CRASHES_DIR / uuid
    dest.mkdir(parents=True, exist_ok=True)
    for dirname in ARTIFACT_DIRS:
        src = ARTIFACTS_ROOT / dirname / f"{uuid}.json"
        if src.exists():
            shutil.copy2(src, dest / f"{dirname}_{uuid}.json")
    lock_data = run_lock.read(uuid)
    if lock_data:
        (dest / "lockfile.json").write_text(
            json.dumps(lock_data, indent=2), encoding="utf-8",
        )
    return dest


def _owning_repo(worktree_path: Path) -> Path | None:
    """The main checkout a worktree belongs to, or None if it can't be read."""
    r = git("rev-parse", "--path-format=absolute", "--git-common-dir",
            cwd=worktree_path)
    if r.returncode != 0:
        return None
    common = Path(r.stdout.strip())
    return common.parent if common.name == ".git" else common


def _remove_worktree(worktree_path: str) -> bool:
    """Attempt to remove a git worktree. Returns True on success or if
    already gone.

    The removal runs in the repo that *owns* the worktree, read back from the
    worktree itself — a run may target any repo, and `git worktree remove`
    from apiary's checkout cannot see another repo's worktrees.
    """
    if not worktree_path:
        return True
    wt = Path(worktree_path)
    if not wt.exists():
        return True
    repo = _owning_repo(wt)
    if repo is None:
        return False
    return git("worktree", "remove", "--force", str(wt), cwd=repo).returncode == 0


def _delete_branches(uuid: str, repo: Path | None = None) -> list[str]:
    """Delete runner branches matching this UUID. Returns list of deleted
    branch names."""
    if repo is None:
        repo = resolve_target_repo()
    result = git("for-each-ref", "--format=%(refname:short)",
                 "refs/heads/runner/", cwd=repo)
    if result.returncode != 0:
        return []

    from .run import _find_runner_branches_from_refs
    refs = result.stdout.splitlines()
    branches = _find_runner_branches_from_refs(refs, uuid)

    deleted = []
    for branch in branches:
        if git("branch", "-D", branch, cwd=repo).returncode == 0:
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


def abort_run(uuid: str) -> str:
    """Full abort sequence for a single crashed run. Returns the summary."""
    lock_data = run_lock.read(uuid)

    if lock_data and not run_lock.is_stale(lock_data):
        pid = lock_data.get("pid", "?")
        raise RuntimeError(
            f"Run {uuid} is still active (PID {pid}) — kill the process first."
        )

    archive_path = _archive_artifacts(uuid)

    worktree_path = (lock_data or {}).get("worktree_path", "")
    # Resolve the owning repo BEFORE the worktree goes away — after removal
    # there is nothing left to read it back from.
    repo = _owning_repo(Path(worktree_path)) if worktree_path and Path(worktree_path).exists() else None
    wt_ok = _remove_worktree(worktree_path)

    branches = _delete_branches(uuid, repo)

    run_lock.delete(uuid)

    summary = _build_summary(uuid, lock_data, archive_path, branches, wt_ok)
    print(summary)

    return summary


def abort_all() -> int:
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
            abort_run(uuid)
            count += 1
        except RuntimeError as e:
            print(f"Skipping {uuid}: {e}", file=sys.stderr)
    return count
