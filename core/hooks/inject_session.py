#!/usr/bin/env python3
"""
PreToolUse hook — injects session_id into conversation context on the
first tool call of each session. This decouples session_id availability
from the budgeter, so any process (startup, scribe, etc.) can use it.

Runs once per session using a flag file. Flags are keyed by session_id
and safe to persist — sessions don't come back — so there is no Stop-hook
cleanup (Stop fires every assistant turn, not at session end; cleaning up
there used to reset the guard, T-2026-117).

Skipped entirely when ``APIARY_RUNNER_SUBPROCESS=1`` is set in the env —
runner stage subprocesses already know their own session_id and don't
need the context injection (#228).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.hook_context import HookResult, context_block, run_standalone
from core.session import SessionId


def run(payload: dict) -> HookResult | None:
    """Return the session-id context block on the session's first tool call."""
    # Runner subprocesses skip this hook entirely (#228).
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        return None

    raw_id = payload.get("session_id", "")
    if not raw_id:
        return None

    try:
        sid = SessionId(raw_id)
    except ValueError:
        return None

    flag_file = sid.flag_path("session_injected")
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    if flag_file.exists():
        return None

    flag_file.write_text("1", encoding="utf-8")
    return HookResult(context=context_block("session", f"session_id: {sid.full}"))


if __name__ == "__main__":
    run_standalone(run)
