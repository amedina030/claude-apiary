#!/usr/bin/env python3
"""
PreToolUse hook — checks installed files against the manifest on the first
tool call of each session. Injects a warning if any files are stale.

Runs once per session: writes a session flag file on first run, exits
immediately on subsequent calls. The flag file is cleaned up by the
companion Stop hook.
"""
import sys
import json
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.session import SessionId
from core.hook_context import context_block, hook_allow, read_payload

CLAUDE_DIR = Path.home() / ".claude"
MANIFEST_PATH = CLAUDE_DIR / ".install-manifest.json"


def file_hash(path):
    """Return SHA-256 hex digest of a file's contents."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (FileNotFoundError, PermissionError):
        return None


def check_manifest():
    """Compare installed files against manifest. Return list of stale file labels."""
    if not MANIFEST_PATH.exists():
        return []

    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, PermissionError):
        return []

    stale = []
    for entry in manifest.get("files", []):
        installed_path = entry.get("installed_path", "")
        expected_hash = entry.get("hash", "")
        label = entry.get("label", installed_path)

        if not installed_path or not expected_hash:
            continue

        current_hash = file_hash(installed_path)
        if current_hash is None:
            stale.append(f"{label} (missing)")
        elif current_hash != expected_hash:
            stale.append(f"{label} (modified)")

    return stale


def main():
    payload = read_payload()

    raw_id = payload.get("session_id", "")
    if not raw_id:
        sys.exit(0)

    try:
        sid = SessionId(raw_id)
    except ValueError:
        sys.exit(0)

    # Once-per-session: check if we already ran for this session.
    flag_file = sid.flag_path("install_checked")
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    if flag_file.exists():
        sys.exit(0)

    # Mark as checked for this session.
    flag_file.write_text("1", encoding="utf-8")

    # Check for stale files.
    stale = check_manifest()
    if stale:
        files_list = ", ".join(stale)
        hook_allow(context_block(
            "install",
            f"Warning: installed files are out of date: {files_list}. "
            f"Run `python setup.py --global` to update."
        ))
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
