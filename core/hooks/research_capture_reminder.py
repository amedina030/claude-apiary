#!/usr/bin/env python3
"""PreToolUse hook — nudge to persist durable web research via the researcher.

The problem (T-2026-225): landscape / comparison / decision-relevant research
done through WebSearch / WebFetch — or inside a spawned subagent — routinely
ends up *only* in the chat transcript and is lost the moment the session
compacts. There is a researcher subsystem built precisely to hold those
findings, but nothing reminds anyone to use it at the moment research happens.

This hook fires a single, deterministic, harness-injected reminder the first
time a research-shaped tool is invoked in a session, pointing at the researcher
as the durable home for anything worth keeping.

Why PreToolUse (not PostToolUse): only PreToolUse / UserPromptSubmit /
SessionStart are documented to inject ``additionalContext`` into the model
context; PostToolUse additionalContext is undocumented and may silently no-op.

Why the ``Agent`` / ``Task`` matcher: a subagent tool call fires this hook in
the PARENT context at spawn time, which closes the gap where research runs
*inside* a subagent (the case that originally bit us). The subagent tool is
named ``Agent`` in this harness and ``Task`` in stock Claude Code — we match
both so the toolkit guardrail is portable; an unmatched name is simply inert.

Gating: once per session, keyed on session_id via a persistent flag file
(``SessionId.flag_path``) — the same mechanism inject_session uses. Flags are
safe to persist (sessions don't come back), so no Stop-hook cleanup is needed
(Stop fires every assistant turn, not at session end — T-2026-117).

Fail-open: every error path degrades to a plain allow — a buggy reminder must
never wedge a tool call.
"""
from __future__ import annotations

# Tools whose invocation signals that durable research may be happening.
#   WebSearch / WebFetch — direct web research in this context.
#   Agent / Task         — a subagent spawn; research may run inside it, so we
#                          remind in the parent at spawn time. Agent is this
#                          harness's name, Task is stock Claude Code's.
RESEARCH_TOOLS = ("WebSearch", "WebFetch", "Agent", "Task")

# Suffix for the once-per-session flag file (SessionId.flag_path).
REMINDED_FLAG = "research_capture_reminded"


def reminder_text() -> str:
    """The context block injected on the first research-shaped tool call.

    Pure + static so it can be asserted in tests without touching stdin.
    """
    return (
        "You're about to do web research (or spawn a subagent that might). "
        "If any finding here is durable — a landscape/comparison, a tool or "
        "library decision, an API contract, anything you'd want on the next "
        "session rather than buried in this chat — persist it via the "
        "researcher instead of leaving it only in the transcript:\n"
        "  - /research add  (the skill), or\n"
        '  - python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" '
        "researcher/cli.py add <topic> <title> [--tags a,b]\n"
        "Skip this for throwaway lookups — it's a nudge for findings worth "
        "keeping, not a mandate to log every search."
    )


def should_remind(tool_name: str, already_reminded: bool) -> bool:
    """Pure decision: fire the reminder iff this is a research-shaped tool and
    the session hasn't been reminded yet. No I/O — fully unit-testable."""
    if already_reminded:
        return False
    return tool_name in RESEARCH_TOOLS


def main():
    """Entry point. Wrapped for fail-open behavior; never raises."""
    try:
        _run()
    except Exception:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from core.hook_context import hook_allow
        hook_allow()


def _run():  # pragma: no cover — exercised via integration; logic lives in
    #                               should_remind / reminder_text (unit-tested).
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.hook_context import context_block, hook_allow, read_payload
    from core.session import SessionId

    # Runner subprocesses are one-shot workers — the nudge is pure token bloat
    # for them (consistent with the other core hooks, #228).
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        hook_allow()
        return

    payload = read_payload()
    tool_name = payload.get("tool_name") or ""
    if tool_name not in RESEARCH_TOOLS:
        hook_allow()
        return

    raw_id = payload.get("session_id", "")
    if not raw_id:
        # No session id → can't gate once-per-session. Stay silent rather than
        # risk nagging on every research call.
        hook_allow()
        return
    try:
        sid = SessionId(raw_id)
    except ValueError:
        hook_allow()
        return

    flag_file = sid.flag_path(REMINDED_FLAG)
    already = flag_file.exists()
    if not should_remind(tool_name, already):
        hook_allow()
        return

    flag_file.parent.mkdir(parents=True, exist_ok=True)
    flag_file.write_text("1", encoding="utf-8")
    hook_allow(context_block("researcher", reminder_text()))


if __name__ == "__main__":  # pragma: no cover
    main()
