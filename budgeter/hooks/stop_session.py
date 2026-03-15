#!/usr/bin/env python3
"""
Stop hook — logs the final tool call's cost and cleans up temp files.

The last tool in a session has no subsequent PRE hook to capture its cost.
This hook reads the final transcript, computes the delta against the last
PRE's baseline, and logs it before cleaning up.
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apis root

from budgeter.lib import logger
from core import flags


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    logger.configure_for_project(cwd)

    if session_id and flags.is_enabled("budgeter-log"):
        baseline = logger.load_baseline(session_id)
        if baseline is not None and baseline.get("prev_tool_name") and baseline.get("prev_tool_name") != "Agent":
            session_entries = logger.read_session_jsonl(transcript_path)
            tokens_now = logger.get_cumulative_tokens(session_entries)
            tokens_delta = max(0, tokens_now - baseline["tokens"])
            context_tokens = baseline.get("context_tokens", 0)
            net_tokens_delta = max(0, tokens_delta - context_tokens)

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "tool_name": baseline.get("prev_tool_name", ""),
                "assistant_message": baseline.get("prev_assistant_message", ""),
                "user_message": baseline.get("user_message", ""),
                "tokens_delta": tokens_delta,
                "context_tokens": context_tokens,
                "net_tokens_delta": net_tokens_delta,
                "turn_number": baseline.get("turn_number", 0),
                "task_turn": baseline.get("task_turn", baseline.get("turn_number", 0)),
                "project": str(Path(transcript_path).parent) if transcript_path else "",
            }
            logger.append_entry(entry)

    if session_id:
        logger.cleanup_session(session_id)


if __name__ == "__main__":
    main()
