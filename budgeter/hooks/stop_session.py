#!/usr/bin/env python3
"""
Stop hook — logs the final tool call's cost, samples the usage limits, and cleans up temp files.

The last tool in a session has no subsequent PRE hook to capture its cost.
This hook reads the final transcript, computes the delta against the last
PRE's baseline, and logs it before cleaning up.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from budgeter.lib import logger
from core import flags
from core.hook_context import run_standalone


def run(payload: dict):
    """Log the final tool call and clean up. Never returns context.

    Always reaches cleanup: a Stop hook that dies before ``cleanup_session``
    leaves the baseline (possibly the corrupt one that killed it) for the next
    session to trip over (review B1). The ``finally`` here keeps that true even
    though the caller — dispatcher or standalone shim — also catches.
    """
    session_id = payload.get("session_id", "")
    try:
        _log_final_call(payload, session_id)
    except Exception as exc:  # noqa: BLE001 — hooks must not crash
        print(f"[budgeter] stop_session failed: {exc!r}", file=sys.stderr)
    try:
        _sample_usage()
    except Exception as exc:  # noqa: BLE001
        print(f"[budgeter] usage sample failed: {exc!r}", file=sys.stderr)
    finally:
        if session_id:
            try:
                logger.cleanup_session(session_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[budgeter] stop_session cleanup failed: {exc!r}", file=sys.stderr)
    return None


def _sample_usage():
    """Record one usage-limit sample if the interval has elapsed.

    On by default everywhere: usage is per account, and this hook fires at
    the end of every assistant turn in every apiary repo, interactive or
    headless, which is when the limits move. ``budgeter-usage-sample-off`` in
    a repo silences it there. The fetcher's 5 s timeout bounds the cost.
    """
    if flags.is_enabled("budgeter-usage-sample-off"):
        return
    from budgeter.lib import usage_samples
    from core.usage_fetcher import fetch_usage

    usage_samples.sample_if_due("hook", fetch_usage)


def _log_final_call(payload, session_id):
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    logger.configure_for_project(cwd)

    if session_id and flags.is_enabled("budgeter-log"):
        baseline = logger.load_baseline(session_id)
        if (
            logger.baseline_comparable(baseline)
            and baseline.get("prev_tool_name")
            and baseline.get("prev_tool_name") != "Agent"
        ):
            session_entries = logger.read_session_jsonl(transcript_path)
            tokens_now = logger.get_cumulative_tokens(session_entries)
            last_input, last_cache, last_create, last_output = logger.get_last_call_tokens(
                session_entries
            )
            # Shrunk total = compaction; the PRE hook writes the marker, the
            # Stop hook must not log a phantom entry against it.
            if tokens_now > baseline["tokens"]:
                entry = logger.build_cost_entry(
                    baseline,
                    session_id,
                    transcript_path,
                    tokens_now,
                    last_input,
                    last_cache,
                    last_create,
                    last_output,
                )
                logger.append_entry(entry)


if __name__ == "__main__":
    run_standalone(run, event="Stop")
