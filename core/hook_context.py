"""
Hook context builder — shared by all PreToolUse/Stop hooks.

Provides consistent formatting for context blocks injected into
Claude's conversation via hookSpecificOutput.additionalContext.
Also consolidates the hook_allow/hook_block response helpers so
every hook produces identical JSON structure.
"""
import json
import sys


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

def hook_allow(context: str = None, event: str = "PreToolUse"):
    """Print a hook allow response and exit 0."""
    out = {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "allow"}}
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
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
    """Read and parse JSON payload from stdin. Returns dict or exits with allow."""
    try:
        return json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        hook_allow()
        sys.exit(0)
