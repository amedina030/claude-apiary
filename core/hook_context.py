"""
Hook context builder — shared by all PreToolUse/Stop hooks.

Provides consistent formatting for context blocks injected into
Claude's conversation via hookSpecificOutput.additionalContext.
Also consolidates the hook_allow/hook_block response helpers so
every hook produces identical JSON structure, and defines the
:class:`HookResult` contract every hook module's ``run(payload)``
returns so ``core/hooks/dispatch.py`` can run them all in one process.
"""
import json
import os
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Context block formatting
# ---------------------------------------------------------------------------

def context_block(namespace: str, *lines: str) -> str:
    """Build a tagged context block.

    >>> context_block("session", "session_id: abc123")
    '[session] session_id: abc123'

    >>> context_block("budgeter", "line one", "line two")
    '[budgeter] line one\\nline two'
    """
    first, *rest = lines
    parts = [f"[{namespace}] {first}"]
    parts.extend(rest)
    return "\n".join(parts)


def join_contexts(*blocks: str) -> str:
    """Join multiple context blocks with double-newline separator."""
    return "\n\n".join(b for b in blocks if b)


# ---------------------------------------------------------------------------
# Hook response helpers
# ---------------------------------------------------------------------------

# The only values Claude Code accepts for PreToolUse ``permissionDecision``.
# ``block`` / ``approve`` are legacy top-level ``decision`` values, not these.
PERMISSION_DECISIONS = ("allow", "deny", "ask")


def hook_allow(context: str = None, event: str = "PreToolUse", decision: str = None):
    """Print a "no objection" hook response and return (exit code stays 0).

    This does **not** vote ``allow``. A hook whose permission decision is
    ``allow`` auto-approves the tool call and silently
    disables default-mode permission prompts for every call it sees — which
    is exactly what this helper did until 2026-08 (review C-1). Now it only
    attaches *context* (``additionalContext``) when given one and otherwise
    prints ``{}``, so Claude Code's normal permission flow decides.

    A hook that genuinely intends to decide passes ``decision`` explicitly
    (``"ask"``, ``"deny"``, or — for one specific, justified call shape, never
    a blanket — ``"allow"``). Anything else raises ``ValueError`` so a typo
    cannot turn into a vote. Hard blocks use :func:`hook_block`.
    """
    if decision is not None and decision not in PERMISSION_DECISIONS:
        raise ValueError(
            f"permissionDecision must be one of {PERMISSION_DECISIONS}, got {decision!r}"
        )
    spec = {}
    if context:
        spec["additionalContext"] = context
    if decision:
        spec["permissionDecision"] = decision
    out = {"hookSpecificOutput": {"hookEventName": event, **spec}} if spec else {}
    print(json.dumps(out))


def hook_block(message: str, event: str = "PreToolUse"):
    """Block the tool call and exit 2, reporting *message* as the reason.

    Claude Code's documented PreToolUse vocabulary is ``allow`` / ``deny`` /
    ``ask`` (``block`` was never a valid value) and the reason field is
    ``permissionDecisionReason``. Exit code 2 is the hard block — it stops the
    call whether or not the JSON is parsed, and feeds stderr back to Claude as
    the reason — so gates emit all three: the JSON ``deny``, the legacy
    top-level ``decision``/``reason`` pair for older clients, and exit 2 with
    the message on stderr. Never returns.
    """
    out = {
        "decision": "block",
        "reason": message,
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
    }
    print(json.dumps(out))
    print(message, file=sys.stderr)
    sys.exit(2)


def read_payload():
    """Read and parse JSON payload from stdin.

    Returns the dict, or on malformed input prints the no-objection response
    and exits 0 (fail open, no permission vote).
    """
    try:
        return json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        hook_allow()
        sys.exit(0)


# ---------------------------------------------------------------------------
# The in-process hook contract (core/hooks/dispatch.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HookResult:
    """What one hook has to say about a payload.

    Every hook module exposes ``run(payload) -> HookResult | None``. Returning
    ``None`` (or ``NO_OPINION``) is the common case: nothing to add, no
    objection — the dispatcher emits ``{}`` when every hook says that.

    - *context*: text merged into the single ``additionalContext`` block the
      dispatcher emits for the event. Never a permission vote (review C-1).
    - *block_reason*: set **only** by a gate that means to stop the call. The
      dispatcher turns it into the ``deny`` JSON + exit 2 that ``hook_block``
      emits, and skips every hook after it.
    """

    context: str | None = None
    block_reason: str | None = None


NO_OPINION = HookResult()


def emit(result: "HookResult | None", event: str = "PreToolUse") -> None:
    """Print the hook response for one :class:`HookResult`.

    A ``block_reason`` goes through :func:`hook_block` (deny JSON, stderr,
    exit 2) and never returns; anything else prints the no-objection /
    context response and returns.
    """
    if result is None:
        result = NO_OPINION
    if result.block_reason:
        hook_block(result.block_reason, event=event)
    hook_allow(result.context or None, event=event)


def run_standalone(run_fn, event: str = "PreToolUse") -> None:
    """Run one hook's ``run(payload)`` as a standalone ``python <hook>.py``.

    The thin shim every hook module keeps under ``if __name__ == "__main__"``
    so it still works when invoked directly (tests, debugging, a settings.json
    entry that predates the dispatcher). Fail-open: an exception inside the
    hook degrades to a no-objection response, exactly as the dispatcher does.
    """
    payload = read_payload()
    try:
        result = run_fn(payload)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — hooks must not crash
        print(f"[{_hook_label(run_fn)}] failed: {exc!r}", file=sys.stderr)
        result = None
    emit(result, event=event)


def _hook_label(run_fn) -> str:
    """The hook's file stem — ``__module__`` is ``__main__`` under the shim."""
    module = sys.modules.get(getattr(run_fn, "__module__", ""), None)
    path = getattr(module, "__file__", None)
    if path:
        return os.path.basename(path).removesuffix(".py")
    return getattr(run_fn, "__module__", "hook")
