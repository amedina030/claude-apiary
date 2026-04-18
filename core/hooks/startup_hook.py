#!/usr/bin/env python3
"""
PreToolUse hook — detects unseen sessions on first tool call.

Only handles unseen session detection (needs transcript_path from
PreToolUse payload). All other startup context (identity, notes,
learnings, CLI reference) is injected earlier via the UserPromptSubmit
hook (startup_prompt_hook.py).

Skipped entirely when ``auto-startup`` flag is off — the user triggers
/startup manually in that mode. Also skipped for runner subprocesses
that set ``APIARY_RUNNER_SUBPROCESS=1``.
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
from core import context_rules as _ctx_rules

CLAUDE_MD_PATH = Path.home() / ".claude" / "CLAUDE.md"


def main():
    try:
        _run()
    except Exception:
        # Hooks must not crash — degrade to no context
        hook_allow()


def _run():
    # Runner subprocesses skip this hook — see startup_prompt_hook.py
    # for the rationale.
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

    lines = []
    if unseen:
        sids = ", ".join(u.get("session_id", "?")[:8] for u in unseen)
        lines.append(f"unseen_sessions: {sids}")
        for u in unseen:
            prefix = u.get("session_id", "?")[:8]
            path = (u.get("transcript_path") or "").replace("\\", "/")
            if path:
                lines.append(f"  {prefix} {path}")
        lines.append(
            "action: invoke /backfill-handoffs to process these. "
            "When the agent completes, silently pipe its <usage> block to the cost logger:"
        )
        lines.append(
            "  echo '<usage>...</usage>' | python ~/.claude/apiary_launch.py "
            "budgeter/log_agent_cost.py --session-id \"<full_session_id>\" --agent \"backfill-handoffs\""
        )
    else:
        lines.append("unseen_sessions: none")

    drift_line = _context_rules_drift_line()
    if drift_line:
        lines.append(drift_line)

    hook_allow(context_block("startup", *lines))


def _context_rules_drift_line() -> str:
    """Return a single 'context-rules drift detected' line, or empty string.

    Layer 2 of #224. Reads ~/.claude/CLAUDE.md, parses any installed managed
    zone, and compares each installed rule's stored hash to its source. Only
    reports rules that are *installed but out of sync* — rules that exist on
    disk but were never installed are a user choice, not drift.

    Never raises. Silent on any error.
    """
    try:
        if not CLAUDE_MD_PATH.exists():
            return ""
        text = CLAUDE_MD_PATH.read_text(encoding="utf-8")
        # source_rules=None suppresses not_installed reporting (per layer 2 spec).
        report = _ctx_rules.compute_drift(text, source_rules=None)
        if not report.zone_present:
            return ""
        issues = (
            len(report.tampered_bodies)
            + len(report.hash_mismatch)
            + len(report.missing_source)
            + (1 if report.stray_text else 0)
        )
        if issues == 0:
            return ""
        return (
            f"[context-rules] drift detected: {issues} rule(s) out of sync — "
            f"run scripts/install_context_rules.py --check for details."
        )
    except Exception:
        return ""


if __name__ == "__main__":
    main()
