#!/usr/bin/env python3
"""
PreToolUse hook — injects session_id into conversation context on the
first tool call of each session. This decouples session_id availability
from the budgeter, so any process (startup, scribe, etc.) can use it.

Runs once per session using a flag file, cleaned up by the companion
Stop hook (check_install_stop.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.session import SessionId
from core.hook_context import context_block, hook_allow, read_payload


def main():
    payload = read_payload()

    raw_id = payload.get("session_id", "")
    if not raw_id:
        hook_allow()
        return

    try:
        sid = SessionId(raw_id)
    except ValueError:
        hook_allow()
        return

    flag_file = sid.flag_path("session_injected")
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    if flag_file.exists():
        hook_allow()
        return

    flag_file.write_text("1", encoding="utf-8")
    hook_allow(context_block("session", f"session_id: {sid.full}"))


if __name__ == "__main__":
    main()
