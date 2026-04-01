#!/usr/bin/env python3
"""
Stop hook — logs the final tool call's cost, writes feedback for the last task,
and cleans up temp files.

The last tool in a session has no subsequent PRE hook to capture its cost.
This hook reads the final transcript, computes the delta against the last
PRE's baseline, and logs it before cleaning up.
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apis root

from budgeter.lib import logger, estimator
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
    config = logger.load_config()

    if session_id and flags.is_enabled("budgeter-log"):
        baseline = logger.load_baseline(session_id)
        if baseline is not None and baseline.get("prev_tool_name") and baseline.get("prev_tool_name") != "Agent":
            session_entries = logger.read_session_jsonl(transcript_path)
            tokens_now = logger.get_cumulative_tokens(session_entries)
            last_input, last_output = logger.get_last_call_tokens(session_entries)
            tokens_delta = max(0, tokens_now - baseline["tokens"])

            prev_input = baseline.get("baseline_input", 0)
            if prev_input > 0:
                input_growth = max(0, last_input - prev_input)
                net_tokens_delta = input_growth + last_output
            else:
                context_tokens = baseline.get("context_tokens", 0)
                net_tokens_delta = max(0, tokens_delta - context_tokens)

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "tool_name": baseline.get("prev_tool_name", ""),
                "assistant_message": baseline.get("prev_assistant_message", ""),
                "user_message": baseline.get("user_message", ""),
                "tokens_delta": tokens_delta,
                "context_tokens": baseline.get("baseline_input", 0) + baseline.get("baseline_output", 0),
                "net_tokens_delta": net_tokens_delta,
                "turn_number": baseline.get("turn_number", 0),
                "task_turn": baseline.get("task_turn", baseline.get("turn_number", 0)),
                "scope_flags": baseline.get("scope_flags", []),
                "project": str(Path(transcript_path).parent) if transcript_path else "",
            }
            logger.append_entry(entry)

        # Write feedback for the last task in the session.
        if baseline is not None:
            feedback_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "task_turn": baseline.get("task_turn", baseline.get("turn_number", 0)),
                "scope_flags": baseline.get("scope_flags", []),
                "score": estimator.score_flags(baseline.get("scope_flags", []), config),
                "predicted_cost": baseline.get("predicted_cost", 0),
                "warning_fired": baseline.get("warning_fired", False),
            }
            logger.append_feedback(feedback_entry)

    if session_id:
        logger.cleanup_session(session_id)


if __name__ == "__main__":
    main()
