#!/usr/bin/env python3
"""
PostToolUse hook — logs Agent token costs directly from the tool response payload.

Agent calls are invisible to the pre_tool_use PRE-to-PRE delta because the subagent
runs in a separate transcript. This hook captures the exact token count from
tool_response.totalTokens and logs it directly.
"""
import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from budgeter.lib import logger
from core import flags


_MAX_STDIN_BYTES = 64 * 1024  # 64 KB — matches log_agent_cost.py cap

# /harden encodes the run's request_id in the Agent description as "[rid:<id>]"
# because the Agent tool has no `env` parameter — there is no LLM-controlled
# mechanism to set APIARY_REQUEST_ID on a spawn. The hook parses it here as
# the authoritative source; the env var is a secondary fallback for callers that
# do control the harness environment.
_RID_PATTERN = re.compile(r'\[rid:([^\]\n\r]{1,128})\]')
_REQUEST_ID_MAX_LEN = 128


def _sanitize_request_id(value: str) -> str:
    """Cap length and reject values with newlines or non-printable characters."""
    if not value:
        return ''
    value = value[:_REQUEST_ID_MAX_LEN]
    if not all(c.isprintable() and c not in '\n\r' for c in value):
        return ''
    return value


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
    # Prefer tool_input.description (the description of THIS specific Agent spawn)
    # over baseline.agent_description, which is session-level and can be stale for
    # round 2+ in /harden — causing wrong per-round attribution in report --by-agent.
    tool_input = payload.get("tool_input") or {}
    agent_desc = tool_input.get("description", "") or (baseline.get("agent_description", "") if baseline else "")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool_name": "Agent",
        "agent_type": agent_desc,
        "assistant_message": baseline.get("prev_assistant_message", "") if baseline else "",
        "user_message": baseline.get("user_message", "") if baseline else "",
        "tokens_delta": safe_tokens,
        "context_tokens": 0,
        "net_tokens_delta": safe_tokens,
        "turn_number": baseline.get("turn_number", 0) if baseline else 0,
        "task_turn": baseline.get("task_turn", 0) if baseline else 0,
        "project": cwd,
    }
    # Prefer the [rid:...] tag embedded in the agent description — it is set by
    # /harden because the Agent tool has no env parameter and APIARY_REQUEST_ID
    # cannot be injected by the LLM. Fall back to the env var for callers that
    # do control the harness environment (e.g. log_agent_cost.py).
    request_id = ''
    if agent_desc:
        m = _RID_PATTERN.search(agent_desc)
        if m:
            request_id = m.group(1)
    if not request_id:
        request_id = _sanitize_request_id(os.environ.get('APIARY_REQUEST_ID', ''))
    if request_id:
        entry['request_id'] = request_id
    logger.append_entry(entry)


if __name__ == "__main__":
    main()
