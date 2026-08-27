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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from budgeter.lib import logger
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

    if session_id and flags.is_enabled("budgeter-log"):
        baseline = logger.load_baseline(session_id)
        if (logger.baseline_comparable(baseline) and baseline.get("prev_tool_name")
                and baseline.get("prev_tool_name") != "Agent"):
            session_entries = logger.read_session_jsonl(transcript_path)
            tokens_now = logger.get_cumulative_tokens(session_entries)
            last_input, last_cache, last_create, last_output = logger.get_last_call_tokens(session_entries)
            # Shrunk total = compaction; the PRE hook writes the marker, the
            # Stop hook must not log a phantom entry against it.
            if tokens_now > baseline["tokens"]:
                entry = logger.build_cost_entry(
                    baseline, session_id, transcript_path, tokens_now,
                    last_input, last_cache, last_create, last_output,
                )
                logger.append_entry(entry)


if __name__ == "__main__":
    main()
