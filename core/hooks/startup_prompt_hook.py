#!/usr/bin/env python3
"""
UserPromptSubmit hook — injects startup context on the first user message.

Always injects: identity, notes summary, learnings, CLI reference.

Unseen session detection (and automatic handoff backfilling) stays in
the PreToolUse startup_hook.py — gated by the ``auto-startup`` flag.
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.session import SessionId
from core.hook_context import context_block, hook_allow, read_payload
from core.startup import run_init, run_summary


def main():
    try:
        _run()
    except Exception:
        # Hooks must not crash — degrade to no context
        hook_allow(event="UserPromptSubmit")


def _run():
    payload = read_payload()

    raw_id = payload.get("session_id", "")
    if not raw_id:
        hook_allow(event="UserPromptSubmit")
        return

    try:
        sid = SessionId(raw_id)
    except ValueError:
        hook_allow(event="UserPromptSubmit")
        return

    # Run-once guard
    flag_file = sid.flag_path("startup_prompt_done")
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    if flag_file.exists():
        hook_allow(event="UserPromptSubmit")
        return
    flag_file.write_text("1", encoding="utf-8")

    parts = []
    first_message = payload.get("message", "")
    cwd = payload.get("cwd", str(PROJECT_ROOT))

    # --- 1. Init: identity ---
    identity = {}
    try:
        init_result = run_init(sid.full, first_message, cwd)
        identity = init_result.get("identity", {})

        parts.append(
            f"identity: role={identity.get('role', 'user')}, "
            f"mission={identity.get('mission', 'general')}, "
            f"registered={identity.get('registered', True)}, "
            f"wants={identity.get('wants_role', 'user')}/{identity.get('wants_mission', 'general')}"
        )
    except Exception:
        parts.append("init: failed (using defaults)")

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

    hook_allow(context_block("startup", *parts), event="UserPromptSubmit")


if __name__ == "__main__":
    main()
