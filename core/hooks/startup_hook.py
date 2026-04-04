#!/usr/bin/env python3
"""
PreToolUse hook — runs deterministic startup work on the first tool call
of each session. Injects identity, summary, learnings, and CLI reference
as a [startup] context block.

Runs after inject_session.py (independent — reads session_id from payload).
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.session import SessionId
from core.hook_context import context_block, hook_allow, read_payload
from core.transcript import read_first_message
from core.startup import run_init, run_summary


def main():
    try:
        _run()
    except Exception:
        # Hooks must not crash — degrade to no context
        hook_allow()


def _run():
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

    parts = []
    cwd = payload.get("cwd", str(PROJECT_ROOT))
    transcript_path = payload.get("transcript_path", "")

    # --- 1. Init: identity + unseen session detection ---
    identity = {}
    try:
        first_msg = read_first_message(transcript_path) if transcript_path else ""
        init_result = run_init(sid.full, first_msg, cwd)
        identity = init_result.get("identity", {})
        unseen = init_result.get("unseen_sessions", [])

        parts.append(
            f"identity: role={identity.get('role', 'user')}, "
            f"mission={identity.get('mission', 'general')}, "
            f"registered={identity.get('registered', True)}, "
            f"wants={identity.get('wants_role', 'user')}/{identity.get('wants_mission', 'general')}"
        )

        if unseen:
            sids = ", ".join(u.get("session_id", "?")[:8] for u in unseen)
            parts.append(f"unseen_sessions: {sids}")
        else:
            parts.append("unseen_sessions: none")
    except Exception:
        parts.append("init: failed (using defaults)")
        parts.append("unseen_sessions: none")

    # --- 2. Summary: active notes, latest handoff ---
    role = identity.get("role", "user")
    mission = identity.get("mission", "general")
    try:
        summary_text = run_summary(cwd, role, mission)
        if summary_text:
            parts.append("")
            parts.append(summary_text)
    except Exception:
        parts.append("summary: failed (non-critical)")

    # --- 3. Learnings (subprocess — scribe/ not importable from core/) ---
    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scribe" / "notes.py"),
             "learnings", "--full"],
            capture_output=True, text=True, timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0 and result.stdout.strip():
            parts.append("")
            parts.append("--- learnings ---")
            parts.append(result.stdout.strip())
    except Exception:
        pass

    # --- 4. CLI reference ---
    try:
        cli_ref = (PROJECT_ROOT / "docs" / "reference" / "cli-tools.md").read_text(encoding="utf-8")
        parts.append("")
        parts.append("--- cli-tools reference ---")
        parts.append(cli_ref.strip())
    except Exception:
        pass

    hook_allow(context_block("startup", *parts))


if __name__ == "__main__":
    main()
