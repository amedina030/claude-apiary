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


def parse_usage(raw: str) -> dict:
    """Extract fields from a <usage> XML block."""
    result = {}
    for tag in ("total_tokens", "tool_uses", "duration_ms"):
        match = re.search(rf"<{tag}>([\d]+)</{tag}>", raw)
        if match:
            result[tag] = int(match.group(1))
    return result


def main():
    parser = argparse.ArgumentParser(description="Log background agent cost")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent", default="background-agent")
    parser.add_argument("--cwd", default="")
    args = parser.parse_args()

    if not flags.is_enabled("budgeter-log"):
        return

    if args.cwd:
        logger.configure_for_project(args.cwd)

    raw = sys.stdin.read()
    usage = parse_usage(raw)
    tokens = usage.get("total_tokens", 0)

    if tokens <= 0:
        return

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": args.session_id,
        "tool_name": "Agent",
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
    logger.append_entry(entry)
    print(f"Logged {tokens} tokens for {args.agent}")


if __name__ == "__main__":
    main()
