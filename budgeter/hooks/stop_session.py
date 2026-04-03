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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from budgeter.lib import logger, estimator
from core import flags


_MAX_STDIN_BYTES = 64 * 1024  # 64 KB — matches log_agent_cost.py cap


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(_MAX_STDIN_BYTES))
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
            last_input, last_cache, last_output = logger.get_last_call_tokens(session_entries)
            tokens_delta = max(0, tokens_now - baseline["tokens"])

            prev_input = baseline.get("baseline_input", 0)
            prev_cache = baseline.get("baseline_cache", 0)
            if prev_input > 0 or prev_cache > 0:
                input_growth = max(0, last_input - prev_input)
                cache_growth = max(0, last_cache - prev_cache)
                net_tokens_delta = input_growth + cache_growth + last_output
            else:
                input_growth = 0
                cache_growth = 0
                context_tokens = baseline.get("context_tokens", 0)
                net_tokens_delta = max(0, tokens_delta - context_tokens)

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "tool_name": baseline.get("prev_tool_name", ""),
                "assistant_message": baseline.get("prev_assistant_message", ""),
                "user_message": baseline.get("user_message", ""),
                "tokens_delta": tokens_delta,
                "context_tokens": baseline.get("baseline_input", 0) + baseline.get("baseline_cache", 0) + baseline.get("baseline_output", 0),
                "net_tokens_delta": net_tokens_delta,
                "input_tokens_delta": input_growth,
                "cache_tokens_delta": cache_growth,
                "output_tokens_delta": last_output,
                "turn_number": baseline.get("turn_number", 0),
                "task_turn": baseline.get("task_turn", baseline.get("turn_number", 0)),
                "scope_flags": baseline.get("scope_flags", []),
                "project": str(Path(transcript_path).parent) if transcript_path else "",
            }
            logger.append_entry(entry)

        # Write feedback for the last task in the session, but only if
        # pre_tool_use hasn't already written a record for the same task
        # (pre_tool_use writes at task boundaries; stop_session covers the
        # final task which has no subsequent PRE hook).
        # Use the atomic read-check-write helper to avoid the TOCTOU race
        # where two concurrent processes both pass the "already written" check.
        if baseline is not None:
            task_turn_val = baseline.get("task_turn", baseline.get("turn_number", 0))
            feedback_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "task_turn": task_turn_val,
                "scope_flags": baseline.get("scope_flags", []),
                "score": estimator.score_flags(baseline.get("scope_flags", []), config),
                "predicted_cost": baseline.get("predicted_cost", 0),
                "warning_fired": baseline.get("warning_fired", False),
            }
            logger.append_feedback_if_not_present(feedback_entry, session_id, task_turn_val)

    if session_id:
        logger.cleanup_session(session_id)


if __name__ == "__main__":
    main()
