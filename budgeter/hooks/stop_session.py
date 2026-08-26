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


def main():
    """Never crash, and always reach cleanup: a Stop hook that dies before
    ``cleanup_session`` leaves the baseline (possibly the corrupt one that
    killed it) for the next session to trip over (review B1)."""
    try:
        payload = json.loads(sys.stdin.buffer.read())
    except json.JSONDecodeError as exc:
        print(f"[budgeter] stop_session: unreadable payload: {exc}", file=sys.stderr)
        sys.exit(0)

    session_id = payload.get("session_id", "")
    try:
        _log_final_call(payload, session_id)
    except Exception as exc:  # noqa: BLE001 — hooks must not crash
        print(f"[budgeter] stop_session failed: {exc!r}", file=sys.stderr)
    finally:
        if session_id:
            try:
                logger.cleanup_session(session_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[budgeter] stop_session cleanup failed: {exc!r}", file=sys.stderr)


def _log_final_call(payload, session_id):
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    logger.configure_for_project(cwd)
    config = logger.load_config()

    if session_id and flags.is_enabled("budgeter-log"):
        baseline = logger.load_baseline(session_id)
        if baseline is not None and baseline.get("prev_tool_name") and baseline.get("prev_tool_name") != "Agent":
            session_entries = logger.read_session_jsonl(transcript_path)
            tokens_now = logger.get_cumulative_tokens(session_entries)
            last_input, last_cache, last_create, last_output = logger.get_last_call_tokens(session_entries)
            if tokens_now != baseline["tokens"]:
                entry = logger.build_cost_entry(
                    baseline, session_id, transcript_path, tokens_now,
                    last_input, last_cache, last_create, last_output,
                )
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


if __name__ == "__main__":
    main()
