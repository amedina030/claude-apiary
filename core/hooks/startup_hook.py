#!/usr/bin/env python3
"""
PreToolUse hook — detects unseen sessions on first tool call.

Only handles unseen session detection (needs transcript_path from
PreToolUse payload). All other startup context (identity, notes,
learnings, CLI reference) is injected earlier via the UserPromptSubmit
hook (startup_prompt_hook.py).

Skipped entirely when ``auto-startup`` flag is off — the user triggers
/startup manually in that mode. Also skipped for pipeline subprocesses
that set ``APIARY_PIPELINE_SUBPROCESS=1``.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.flags import is_enabled
from core.session import SessionId, load_identity
from core.hook_context import context_block, hook_allow, read_payload
from core.startup import get_unseen_sessions
from core.utils.project import get_project_key


def main():
    try:
        _run()
    except Exception:
        # Hooks must not crash — degrade to no context
        hook_allow()


def _run():
    # Pipeline subprocesses skip this hook — see startup_prompt_hook.py
    # for the rationale.
    if os.environ.get("APIARY_PIPELINE_SUBPROCESS") == "1":
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

    # Run-once guard
    flag_file = sid.flag_path("startup_done")
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    if flag_file.exists():
        hook_allow()
        return
    flag_file.write_text("1", encoding="utf-8")

    # Skip when auto-startup is off — user triggers /startup manually
    if not is_enabled("auto-startup"):
        hook_allow()
        return

    cwd = payload.get("cwd", str(PROJECT_ROOT))

    # Load identity written by the prompt hook
    identity = load_identity(sid)
    wants_role = identity.get("wants_role", "user")
    wants_mission = identity.get("wants_mission", "general")

    # Detect unseen sessions
    project_key = get_project_key(cwd)
    try:
        unseen = get_unseen_sessions(sid.full, wants_role, wants_mission, project_key)
    except Exception:
        unseen = []

    if unseen:
        sids = ", ".join(u.get("session_id", "?")[:8] for u in unseen)
        hook_allow(context_block("startup", f"unseen_sessions: {sids}"))
    else:
        hook_allow(context_block("startup", "unseen_sessions: none"))


if __name__ == "__main__":
    main()
