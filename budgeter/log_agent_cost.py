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
from core.session import SessionId


_MAX_STDIN_BYTES = 64 * 1024  # 64 KB — far more than any <usage> block needs

# request_id may be a UUID or a short slug-style id; allow alphanumerics + hyphen + underscore
_REQUEST_ID_RE = re.compile(r'^[0-9a-zA-Z_\-]{1,64}$')


def _validate_session_id(session_id: str) -> str:
    """Raise ValueError if session_id is not a valid UUID or 8-char hex prefix."""
    SessionId(session_id)  # raises ValueError on bad format
    return session_id


def _validate_request_id(request_id: str) -> str:
    """Raise ValueError if request_id contains unexpected chars."""
    if not request_id or not _REQUEST_ID_RE.match(request_id):
        raise ValueError(f"Invalid request_id: {request_id!r}")
    return request_id


def parse_usage(raw: str) -> dict:
    """Extract fields from a <usage> XML block.

    Recognizes both the legacy 3-field format (total_tokens/tool_uses/duration_ms)
    and the extended 7-field format emitted by runner/cost_emit.py which
    breaks out per-category token counts for weighted reporting.
    """
    result = {}
    tags = (
        "total_tokens",
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "tool_uses",
        "duration_ms",
    )
    for tag in tags:
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
                        help="Optional grouping id for multi-call chains (e.g. one runner run)")
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

    # Per-category breakdown for weighted reporting. Runner stages emit
    # the extended <usage> format via runner/cost_emit.py; legacy 3-field
    # callers simply leave these at 0 and fall back to net_delta in
    # budgeter/report.py's weighted_delta().
    input_tokens = usage.get("input_tokens", 0)
    cache_read_tokens = usage.get("cache_read_input_tokens", 0)
    cache_create_tokens = usage.get("cache_creation_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    # Cache-creation writes cost ~1.25x fresh input; lump them into the
    # fresh-input bucket since the budgeter's weighted model only has
    # input/cache/output weights. Close enough for cap accounting.
    input_tokens_delta = input_tokens + cache_create_tokens

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool_name": "Agent",
        "agent_type": args.agent,
        "assistant_message": f"[background] {args.agent}",
        "user_message": "",
        "tokens_delta": tokens,
        "input_tokens_delta": input_tokens_delta,
        "cache_tokens_delta": cache_read_tokens,
        "output_tokens_delta": output_tokens,
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
