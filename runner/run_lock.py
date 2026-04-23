"""Run lockfile protocol for crash detection (T-2026-128).

Each runner invocation writes a lockfile at runner/locks/<uuid>.lock
containing process metadata. On clean exit the lockfile is deleted.
If the process crashes, the lockfile persists and is detected as stale
by subsequent invocations.
"""
from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

from .config_loader import get as cfg
from .target_repo import locks_dir

LOCKS_DIR = locks_dir()

_FIELDS = ("pid", "hostname", "stage", "step_number", "started_at", "worktree_path")


def _lock_path(uuid: str) -> Path:
    return LOCKS_DIR / f"{uuid}.lock"


def write(uuid: str, stage: str = "", step_number: int = 0,
          worktree_path: str = "") -> Path:
    """Create or overwrite a lockfile for the given run UUID."""
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path(uuid)
    data = {
        "pid": os.getpid(),
        "hostname": platform.node(),
        "stage": stage,
        "step_number": step_number,
        "started_at": time.time(),
        "worktree_path": worktree_path,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def update(uuid: str, *, stage: str | None = None,
           step_number: int | None = None) -> None:
    """Update stage/step_number in an existing lockfile without touching
    pid, hostname, started_at, or worktree_path."""
    path = _lock_path(uuid)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if stage is not None:
        data["stage"] = stage
    if step_number is not None:
        data["step_number"] = step_number
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def delete(uuid: str) -> None:
    """Remove the lockfile on clean exit."""
    path = _lock_path(uuid)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read(uuid: str) -> dict | None:
    """Read and parse a lockfile. Returns None if missing or corrupt."""
    path = _lock_path(uuid)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID exists."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def is_stale(data: dict) -> bool:
    """Determine whether a lockfile represents a crashed run.

    Stale when:
    - PID is not alive, OR
    - PID is alive but lockfile age exceeds max_stage_timeout (covers
      Windows PID recycling where the original process died and a new
      unrelated process reused the PID).
    """
    pid = data.get("pid")
    if not _pid_alive(pid):
        return True
    started_at = data.get("started_at", 0)
    max_timeout = cfg("orchestrator", "stage_timeout", 3600)
    return (time.time() - started_at) > max_timeout


def scan_stale() -> list[dict]:
    """Scan runner/locks/ for stale lockfiles.

    Returns a list of dicts, each with 'uuid' added alongside the
    original lockfile fields.
    """
    if not LOCKS_DIR.exists():
        return []
    results = []
    for lock_file in LOCKS_DIR.glob("*.lock"):
        uuid = lock_file.stem
        data = read(uuid)
        if data is None:
            results.append({"uuid": uuid, "_corrupt": True})
            continue
        if is_stale(data):
            entry = dict(data)
            entry["uuid"] = uuid
            results.append(entry)
    return results
