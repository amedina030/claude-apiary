#!/usr/bin/env python3
"""
PreToolUse hook — injects session_id into conversation context on the
first tool call of each session. This decouples session_id availability
from the budgeter, so any process (startup, scribe, etc.) can use it.

Runs once per session using a flag file, cleaned up by the companion
Stop hook (check_install_stop.py).
"""
import sys
import json
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SESSION_FLAG_DIR = CLAUDE_DIR / "tmp"


def hook_allow(context):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    out["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(out))


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    if not session_id:
        sys.exit(0)

    SESSION_FLAG_DIR.mkdir(parents=True, exist_ok=True)
    flag_file = SESSION_FLAG_DIR / f"{session_id}_session_injected"
    if flag_file.exists():
        sys.exit(0)

    flag_file.write_text("1", encoding="utf-8")
    hook_allow(f"[session] session_id: {session_id}")


if __name__ == "__main__":
    main()
