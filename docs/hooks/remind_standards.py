#!/usr/bin/env python3
"""
PreToolUse hook — reminds Claude to consult standards docs when writing code or docs.

Fires on Write and Edit tools. Injects a one-line reminder pointing to the relevant
standards doc based on the file being modified. Tracks which reminders have already
been shown this session to avoid repetition.
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from core.hook_context import context_block, hook_allow, read_payload
from core.session import SessionId

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# Map file patterns to standards docs
STANDARDS = {
    "code": "docs/standards/code-style.md",
    "doc": "docs/standards/doc-style.md",
    "tool": "docs/standards/new-tool-checklist.md",
}


def _flag_path(session_id: str, reminder_key: str) -> Path:
    """Path to a flag file tracking whether a reminder was already shown."""
    sid = SessionId(session_id)
    return sid.flag_path(f"remind_{reminder_key}")


def _already_reminded(session_id: str, reminder_key: str) -> bool:
    """Check if this reminder was already shown this session."""
    return _flag_path(session_id, reminder_key).exists()


def _mark_reminded(session_id: str, reminder_key: str) -> None:
    """Mark a reminder as shown for this session."""
    fp = _flag_path(session_id, reminder_key)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("1", encoding="utf-8")


def _classify_file(file_path: str) -> str | None:
    """Determine which standard applies to a file path. Returns key or None."""
    try:
        fp = Path(file_path).resolve()
    except (ValueError, OSError):
        return None

    # Must be within the repo
    try:
        rel = fp.relative_to(REPO_ROOT)
    except ValueError:
        return None

    rel_posix = rel.as_posix()

    # Doc files under docs/
    if rel_posix.startswith("docs/") and fp.suffix == ".md":
        return "doc"

    # Python files
    if fp.suffix == ".py":
        # Check if this is a new top-level tool dir (parent is repo root, dir is new)
        parts = rel.parts
        if len(parts) >= 1:
            top_dir = REPO_ROOT / parts[0]
            known_dirs = {"budgeter", "scribe", "core", "docs", ".claude"}
            if parts[0] not in known_dirs and top_dir.is_dir():
                return "tool"
        return "code"

    return None


def main():
    try:
        payload = read_payload()

        tool_name = payload.get("tool_name", "")
        if tool_name not in ("Write", "Edit"):
            hook_allow()
            return

        session_id = payload.get("session_id", "")
        if not session_id:
            hook_allow()
            return

        # Extract file path from tool input
        tool_input = payload.get("tool_input", {})
        file_path = tool_input.get("file_path", "")
        if not file_path:
            hook_allow()
            return

        category = _classify_file(file_path)
        if not category:
            hook_allow()
            return

        # Check if already reminded this session
        if _already_reminded(session_id, category):
            hook_allow()
            return

        # Mark as reminded and inject context
        _mark_reminded(session_id, category)
        standard_path = STANDARDS[category]
        reminder = f"Standards: read {standard_path} before proceeding (first write of this type this session)"
        hook_allow(context_block("docs", reminder))

    except Exception:
        # Hooks must not crash
        hook_allow()


if __name__ == "__main__":
    main()
