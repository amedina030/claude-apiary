#!/usr/bin/env python3
"""
UserPromptSubmit + PreToolUse hook — keeps the compass rule table near the
active turn (D-2026-62 step 2, T-2026-320).

The whole ``<state-dir>/compass/rules.md`` goes out once, in the startup block
(``core/hooks/startup_prompt_hook.py``). This module does the two smaller
deliveries around it:

* **The pin** (UserPromptSubmit). Every ``PIN_EVERY``-th user message,
  inject the compact form — the principle rows plus the self-check, about 250
  tokens — so the rules stay within reach of the turn Claude is composing and
  survive context compaction. The first message carries the full table in the
  startup block, so it never pins. The hook counts, not the model: the
  per-session message count lives in a flag file under ``session-tmp/`` and
  is incremented here on every call.
* **Hook-point rules** (PreToolUse, the minor path). Before an ``Agent`` /
  ``Task`` spawn inject J5 (usage cost, once per session and agent); before
  ``AskUserQuestion`` inject O3 (ask in prose, every time — each call is the
  miss the rule names).

Both read the rendered ``rules.md`` back (``compass.rules.parse_rules_md``)
rather than rebuilding it, so what is injected is exactly what the file says:
a manual override changes the text, a dropped id injects nothing, and a
target with no ``rules.md`` gets nothing at all.

Runner subprocesses (``APIARY_RUNNER_SUBPROCESS=1``) get ``rules.md`` as a
prompt preamble from ``runner/claude_subprocess.py`` instead, so both paths
return ``None`` there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.hook_context import HookResult, context_block, run_standalone
from core.sanitizer import sanitize_and_report
from core.session import SessionId
from core.utils.gitutil import git_root
from core.utils.state import find_state_dir, state_dir_from_env

# Mirror of runner/claude_subprocess.py::RUNNER_SUBPROCESS_ENV_VAR — hardcoded
# rather than imported to avoid a core -> runner dependency edge.
RUNNER_SUBPROCESS_ENV_VAR = "APIARY_RUNNER_SUBPROCESS"

#: Tool name -> the rule injected before that tool runs.
TOOL_RULES = {"Agent": "J5", "Task": "J5", "AskUserQuestion": "O3"}
#: Tools whose rule is injected once per session (and agent), not on every call.
ONCE_PER_SESSION_TOOLS = frozenset({"Agent", "Task"})
#: The dispatcher matcher for the PreToolUse registration.
MATCHER = "Agent|Task|AskUserQuestion"

#: Pin on every N-th user message. Ten to start (2026-09-06); may be revisited.
PIN_EVERY = 10
#: Flag file holding the session's user-message count (the hook's counter).
PIN_COUNTER = "compass_pin_count"
CONTEXT_NAMESPACE = "compass"


def rules_path(cwd: str | None) -> Path | None:
    """``<state-dir>/compass/rules.md`` for the session's repo, or ``None``.

    The launcher's ``APIARY_TARGET_STATE_DIR`` is the pre-resolved answer;
    the pins of the cwd's git root are the fallback (a standalone run). Never
    the legacy in-repo layout: a repo apiary does not manage has no rules.
    """
    state_dir = state_dir_from_env()
    if state_dir is None:
        root = git_root(Path(cwd)) if cwd else None
        if root is None:
            return None
        state_dir = find_state_dir(root)
    if state_dir is None:
        return None
    return state_dir / "compass" / "rules.md"


def _parsed_rules(cwd: str | None) -> dict | None:
    path = rules_path(cwd)
    if path is None or not path.is_file():
        return None
    from compass import rules

    parsed = rules.parse_rules_md(path.read_text(encoding="utf-8"))
    return parsed if parsed.get("rows") else None


def _session(payload: dict) -> SessionId | None:
    raw = str(payload.get("session_id") or "").strip()
    if not raw:
        return None
    try:
        return SessionId(raw)
    except ValueError:
        return None


def bump_counter(path: Path) -> int:
    """Increment the on-disk user-message count and return the new value.

    A missing or unreadable file counts from zero again; a write failure is
    swallowed (the pin then fires on the next readable count), because a
    hook must never fail a user message over a scratch file.
    """
    try:
        count = int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        count = 0
    count += 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(count), encoding="utf-8")
    except OSError:
        pass
    return count


def _pin(payload: dict) -> HookResult | None:
    sid = _session(payload)
    if sid is None:
        return None
    count = bump_counter(sid.flag_path(PIN_COUNTER))
    if count % PIN_EVERY != 0:
        # Message 1 carries the whole table in the startup block; the pin
        # lands on messages 10, 20, 30, ...
        return None
    parsed = _parsed_rules(payload.get("cwd"))
    if parsed is None:
        return None
    from compass import rules

    scrubbed, _hits = sanitize_and_report(rules.pin_text(parsed))
    return HookResult(context=context_block(CONTEXT_NAMESPACE, scrubbed))


def _tool_rule(payload: dict, tool_name: str) -> HookResult | None:
    rule_id = TOOL_RULES.get(tool_name)
    if rule_id is None:
        return None
    flag = None
    if tool_name in ONCE_PER_SESSION_TOOLS:
        sid = _session(payload)
        if sid is not None:
            agent_id = str(payload.get("agent_id") or "").strip()
            suffix = f"compass_rule_{rule_id}" + (f"_{agent_id}" if agent_id else "")
            flag = sid.flag_path(suffix)
            if flag.exists():
                return None
    parsed = _parsed_rules(payload.get("cwd"))
    if parsed is None:
        return None
    from compass import rules

    line = rules.rule_line(parsed, rule_id)
    if line is None:
        return None
    if flag is not None:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    scrubbed, _hits = sanitize_and_report(f"rule before {tool_name}: {line}")
    return HookResult(context=context_block(CONTEXT_NAMESPACE, scrubbed))


def run(payload: dict) -> HookResult | None:
    """Pin the rules to a user message, or inject one rule before a tool call."""
    if os.environ.get(RUNNER_SUBPROCESS_ENV_VAR) == "1":
        return None
    tool_name = str(payload.get("tool_name") or "")
    if tool_name:
        return _tool_rule(payload, tool_name)
    event = payload.get("hook_event_name")
    if event not in (None, "UserPromptSubmit"):
        return None
    return _pin(payload)


if __name__ == "__main__":
    run_standalone(run, event="UserPromptSubmit")
