#!/usr/bin/env python3
"""
PreToolUse hook — injects session_id into conversation context on the
first tool call of each session. This decouples session_id availability
from the budgeter, so any process (startup, scribe, etc.) can use it.

Runs once per session using a flag file, cleaned up by the companion
Stop hook (check_install_stop.py).

Skipped entirely when ``APIARY_RUNNER_SUBPROCESS=1`` is set in the env —
runner stage subprocesses already know their own session_id and don't
need the context injection (#228).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.session import SessionId
from core.hook_context import context_block, hook_allow, read_payload


def main():
    # Runner subprocesses skip this hook entirely (#228).
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        hook_allow()
        return

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
