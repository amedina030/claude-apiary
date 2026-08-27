#!/usr/bin/env python3
"""PostToolUse hook that injects two context-rule reminders when a Bash
tool call fails: the 'recover_from_trivial_errors' behavioral rule and the
'Errors Signal Doc Gaps' principle from docs/_framework.md.

Rationale (#225): CLAUDE.md and docs/_framework.md are loaded once at session
start. By the time a trivial bash error happens mid-session, those rules are
thousands of tokens behind in context and the model often drifts. This hook
fires at the exact moment enforcement is needed — right after a failed tool
call — and re-states the two rules so they sit directly on top of the failure.

Failure heuristics (any of):
  - tool_response.interrupted == True
  - tool_response.is_error == True
  - tool_response is a string (Claude Code's Bash error envelope)
  - non-zero exit_code / returncode field
  - stderr or stdout contains a Python traceback

The hook never blocks a tool call and never raises — it fails open on any
internal error so a buggy reminder never wedges the session.

Skipped entirely when ``APIARY_RUNNER_SUBPROCESS=1`` is set in the env —
runner stage subprocesses are one-shot workers and the behavioral-rule
nudge is just token bloat for them (#228).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.hook_context import HookResult, run_standalone

REMINDER = (
    "[context-rule: recover_from_trivial_errors] A tool call just failed. "
    "If the fix is obvious from the error message and doesn't change your "
    "plan, fix and retry in the same turn without narrating. Only surface "
    "if the error reveals a wrong assumption or needs a real user decision."
    "\n\n"
    "[docs/_framework.md: Errors Signal Doc Gaps] Every avoidable error is "
    "a signal that either (a) the docs weren't consulted, or (b) the docs "
    "don't cover this usage. After recovering, ask: could documentation "
    "have prevented this? If yes, either update the relevant doc or fix "
    "the loading/consultation pattern so the doc gets read next time. The "
    "goal is that the same category of error never happens twice."
)

TRACEBACK_MARKER = "Traceback (most recent call last):"

# Marks that indicate the failure was a hook-policy denial, not a trivial
# tool error. The behavioral rule does not apply to denials — the user (via
# a hook) explicitly blocked the call, so retrying silently would be wrong.
HOOK_DENY_MARKERS = (
    "denied this tool",
    "Hook PreToolUse",
    "permissionDecision",
    "Blocked by",
)


def _is_hook_denial(text: str) -> bool:
    return any(m in text for m in HOOK_DENY_MARKERS)


def _looks_like_failure(tool_response) -> bool:
    """Return True if the tool_response envelope indicates a real failure
    (not success, not a hook-policy denial).
    """
    # String-form response: Claude Code sometimes returns a bare error string.
    if isinstance(tool_response, str):
        if not tool_response:
            return False
        if _is_hook_denial(tool_response):
            return False
        return TRACEBACK_MARKER in tool_response or "error" in tool_response.lower()

    if not isinstance(tool_response, dict):
        return False

    # Explicit failure flags from the tool envelope.
    if tool_response.get("interrupted") is True:
        return True
    if tool_response.get("is_error") is True or tool_response.get("isError") is True:
        return True

    # Some tool envelopes carry a numeric exit code.
    for key in ("exit_code", "exitCode", "returncode", "return_code"):
        code = tool_response.get(key)
        if isinstance(code, int) and code != 0:
            return True

    # Fall back to scanning stdout/stderr for a traceback. Skip if the text
    # is actually a hook denial — those are not trivial errors.
    combined = ""
    for key in ("stdout", "stderr", "output", "content", "error"):
        v = tool_response.get(key)
        if isinstance(v, str):
            combined += v + "\n"
    if combined and _is_hook_denial(combined):
        return False
    if TRACEBACK_MARKER in combined:
        return True

    return False


def run(payload: dict) -> HookResult | None:
    """Return the behavioural reminder when a Bash call just failed."""
    # Runner subprocesses skip this hook entirely (#228).
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        return None

    if payload.get("tool_name") != "Bash":
        return None

    if not _looks_like_failure(payload.get("tool_response")):
        return None

    return HookResult(context=REMINDER)


if __name__ == "__main__":
    run_standalone(run, event="PostToolUse")
