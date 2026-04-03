#!/usr/bin/env python3
"""
PostToolUse hook — logs Agent token costs directly from the tool response payload.

Agent calls are invisible to the pre_tool_use PRE-to-PRE delta because the subagent
runs in a separate transcript. This hook captures the exact token count from
tool_response.totalTokens and logs it directly.
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from budgeter.lib import logger
from core import flags


_MAX_STDIN_BYTES = 64 * 1024  # 64 KB — matches log_agent_cost.py cap


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(_MAX_STDIN_BYTES))
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    if tool_name != "Agent":
        sys.exit(0)

    if not flags.is_enabled("budgeter-log"):
        sys.exit(0)

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    logger.configure_for_project(cwd)

    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, dict):
        sys.exit(0)
    total_tokens = max(0, tool_response.get("totalTokens", 0))
    if total_tokens == 0:
        sys.exit(0)

    baseline = logger.load_baseline(session_id)

    safe_tokens = max(0, total_tokens)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool_name": "Agent",
        "assistant_message": baseline.get("prev_assistant_message", "") if baseline else "",
        "user_message": baseline.get("user_message", "") if baseline else "",
        "tokens_delta": safe_tokens,
        "context_tokens": 0,
        "net_tokens_delta": safe_tokens,
        "turn_number": baseline.get("turn_number", 0) if baseline else 0,
        "task_turn": baseline.get("task_turn", 0) if baseline else 0,
        "project": cwd,
    }
    logger.append_entry(entry)


if __name__ == "__main__":
    main()
