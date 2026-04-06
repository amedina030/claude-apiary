#!/usr/bin/env python3
"""
Log background agent token costs to the budgeter usage log.

Called from the main conversation after a background agent completes,
since background agents don't trigger PostToolUse hooks.

Reads the raw <usage> block from stdin to avoid LLM transcription errors.

Usage:
    echo '<usage><total_tokens>14701</total_tokens><tool_uses>2</tool_uses><duration_ms>7559</duration_ms></usage>' \
        | python budgeter/log_agent_cost.py --session-id <session_id> --agent startup [--cwd <dir>]
"""
import argparse
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from budgeter.lib import logger
from core import flags


_MAX_STDIN_BYTES = 64 * 1024  # 64 KB — far more than any <usage> block needs

# Allow only characters that are safe as path components: hex digits and hyphens
# (UUID format). Reject anything that could navigate the filesystem.
_SESSION_ID_RE = re.compile(r'^[0-9a-fA-F\-]{1,64}$')

# request_id may be a UUID or a short slug-style id; allow alphanumerics + hyphen + underscore
_REQUEST_ID_RE = re.compile(r'^[0-9a-zA-Z_\-]{1,64}$')


def _validate_session_id(session_id: str) -> str:
    """Raise ValueError if session_id contains path-traversal or unexpected chars."""
    if not session_id or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return session_id


def _validate_request_id(request_id: str) -> str:
    """Raise ValueError if request_id contains unexpected chars."""
    if not request_id or not _REQUEST_ID_RE.match(request_id):
        raise ValueError(f"Invalid request_id: {request_id!r}")
    return request_id


def parse_usage(raw: str) -> dict:
    """Extract fields from a <usage> XML block."""
    result = {}
    for tag in ("total_tokens", "tool_uses", "duration_ms"):
        # Limit digit match to 15 digits to avoid ReDoS / integer overflow
        match = re.search(rf"<{tag}>(\d{{1,15}})</{tag}>", raw)
        if match:
            result[tag] = int(match.group(1))
    return result


def main():
    parser = argparse.ArgumentParser(description="Log background agent cost")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent", default="background-agent")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--request-id", default="",
                        help="Optional grouping id for multi-call chains (e.g. one pipeline run)")
    args = parser.parse_args()

    if not flags.is_enabled("budgeter-log"):
        return

    try:
        session_id = _validate_session_id(args.session_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    request_id = ""
    if args.request_id:
        try:
            request_id = _validate_request_id(args.request_id)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.cwd:
        logger.configure_for_project(args.cwd)

    raw = sys.stdin.read(_MAX_STDIN_BYTES)
    usage = parse_usage(raw)
    tokens = usage.get("total_tokens", 0)

    if tokens <= 0:
        return

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool_name": "Agent",
        "agent_type": args.agent,
        "assistant_message": f"[background] {args.agent}",
        "user_message": "",
        "tokens_delta": tokens,
        "context_tokens": 0,
        "net_tokens_delta": tokens,
        "turn_number": 0,
        "task_turn": 0,
        "scope_flags": [],
        "project": args.cwd,
    }
    if request_id:
        entry["request_id"] = request_id
    logger.append_entry(entry)
    print(f"Logged {tokens} tokens for {args.agent}")


if __name__ == "__main__":
    main()
