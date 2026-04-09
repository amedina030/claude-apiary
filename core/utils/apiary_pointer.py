"""Apiary repo pointer file — shared by install-time writers and hook readers.

The apiary installation writes ``~/.claude/apiary.json`` at global install
time so every hook, regardless of which repo a Claude Code session was
started in, can locate the apiary code repo. This module is the single
source of truth for the pointer file's path, schema, and read/write
semantics.

Schema (version 1)::

    {
        "version": 1,
        "repo_path": "<absolute path to the apiary repo root>"
    }

Read/write helpers tolerate a missing file and return ``None`` for malformed
content rather than raising — hooks must stay robust even if the pointer is
stale, truncated, or hand-edited.

Part of the in-repo scribe state migration (decision #269, todo #263).
"""
import json
import os
import sys
from pathlib import Path

DEFAULT_POINTER_PATH = Path.home() / ".claude" / "apiary.json"
POINTER_VERSION = 1


def _resolve_path(pointer_path: Path | None) -> Path:
    return Path(pointer_path) if pointer_path is not None else DEFAULT_POINTER_PATH


def write_pointer(repo_path: Path, *, pointer_path: Path | None = None) -> Path:
    """Write the pointer file atomically and return the path written.

    The parent directory is created if missing. The write is done via a
    ``.tmp`` sibling + ``os.replace`` so concurrent readers never observe a
    truncated JSON file. ``repo_path`` is resolved to an absolute path on
    the current machine before being stored.
    """
    target = _resolve_path(pointer_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": POINTER_VERSION,
        "repo_path": str(Path(repo_path).resolve()),
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with open(tmp, "r+b") as fh:
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(target)
    return target


def read_pointer(pointer_path: Path | None = None) -> dict | None:
    """Return the pointer payload, or None if missing / malformed.

    Never raises. Prints a single warning to stderr when the file is
    present but unreadable or fails schema validation so operators can
    see the cause during ``setup.py --check`` runs.
    """
    target = _resolve_path(pointer_path)
    if not target.is_file():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warning: apiary pointer {target} unreadable: {exc}", file=sys.stderr)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"warning: apiary pointer {target} is not valid JSON: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print(f"warning: apiary pointer {target} is not a JSON object", file=sys.stderr)
        return None
    if not isinstance(payload.get("repo_path"), str) or not payload["repo_path"]:
        print(f"warning: apiary pointer {target} missing repo_path", file=sys.stderr)
        return None
    return payload


def get_repo_path(pointer_path: Path | None = None) -> Path | None:
    """Return the apiary repo root from the pointer file, or None.

    Returns None when the pointer is missing, malformed, or points at a
    path that no longer exists on disk — callers should treat all three
    identically and fall back to their own discovery logic.
    """
    payload = read_pointer(pointer_path)
    if payload is None:
        return None
    candidate = Path(payload["repo_path"])
    if not candidate.is_dir():
        return None
    return candidate


def remove_pointer(pointer_path: Path | None = None) -> bool:
    """Delete the pointer file if it exists. Returns True when removed."""
    target = _resolve_path(pointer_path)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
