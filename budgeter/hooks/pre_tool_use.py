#!/usr/bin/env python3
"""
PreToolUse hook — logs the previous tool's cost.

At PRE time the transcript has already been updated by the API call that processed
the previous tool's result. The delta (tokens_now - baseline.tokens) is therefore
the true cost of the previous tool call, attributed correctly.

The final tool in a session is logged by the Stop hook.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from budgeter.lib import estimator, logger
from core import flags
from core.hook_context import HookResult, context_block, join_contexts, run_standalone
from core.session import SessionId


def run(payload: dict):
    """Log the previous tool call's cost; return the session-length nudge.

    Never crashes the tool call: the dispatcher (and the standalone shim)
    catch and log anything raised here (review B1).
    """
    tool_name = payload.get("tool_name", "")
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    logger.configure_for_project(cwd)
    config = logger.load_config()

    if tool_name not in config.get("monitored_tools", ["Agent", "Bash"]):
        return None

    session_entries = logger.read_session_jsonl(transcript_path)
    tokens_now = logger.get_cumulative_tokens(session_entries)
    last_input, last_cache, last_create, last_output = logger.get_last_call_tokens(session_entries)
    assistant_message = logger.get_last_assistant_message(session_entries)
    turn_number = logger.get_user_turn_number(session_entries)

    # Log the PREVIOUS tool's cost. The baseline was written at the previous PRE,
    # and since then Claude made one API call (to process the previous tool's result
    # and decide to call this tool) — that delta is the previous tool's true cost.
    baseline = logger.load_baseline(session_id)

    # task_turn anchors every tool call of one task to the user turn that
    # started it. The first monitored call of a new turn opens a new task;
    # later calls in the same turn stay on the baseline's anchor.
    #
    # This used to also chain across turns via a `[CONT]` marker Claude was
    # asked to prefix mid-task questions with. The Stop hook deletes the
    # baseline at the end of *every* assistant turn, so the branch could
    # almost never fire — 11 of 25,027 real entries — while the instruction
    # cost context in every session (review B7). Both are gone.
    prev_turn = baseline.get("turn_number", 0) if baseline is not None else 0
    is_new_turn = turn_number > prev_turn

    if is_new_turn or baseline is None:
        task_turn = turn_number
    else:
        # Same turn as before: always use the baseline's task_turn directly.
        # Use "task_turn" key; fall back to "turn_number" only when the baseline
        # predates the task_turn field (old records).  Never fall back to the
        # current turn_number, which would misattribute costs to a different task.
        task_turn = baseline["task_turn"] if "task_turn" in baseline else baseline.get("turn_number", turn_number)

    # Capture user prompt on the first tool call of a new task; inherit it otherwise.
    if is_new_turn:
        user_message = logger.get_user_message_at_turn(session_entries, turn_number)
    else:
        user_message = baseline.get("user_message", "") if baseline is not None else ""

    # A baseline written by an older counting scheme is kept for task/turn
    # continuity but never compared against (its numbers mean something else).
    comparable = logger.baseline_comparable(baseline)
    compacted = (comparable
                 and tokens_now < baseline["tokens"]
                 and baseline.get("prev_tool_name") != "Agent")

    if flags.is_enabled("budgeter-log"):
        # Log a compaction marker when token count drops (transcript was rewritten).
        # The cost of this tool call is lost — accept the gap.
        if compacted:
            logger.append_entry({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "tool_name": "[compaction]",
                "assistant_message": f"Context compacted: {baseline['tokens']:,} -> {tokens_now:,} tokens",
                "user_message": "",
                "tokens_delta": 0,
                "context_tokens": 0,
                "net_tokens_delta": 0,
                "turn_number": turn_number,
                "task_turn": task_turn,
                "project": "",
                "_marker": True,  # force-write despite zero deltas
            })

        # No API call happened since the last PRE (parallel tool calls in one
        # assistant turn): there is no cost to attribute, so log nothing.
        # Logging last_output here created phantom entries — 25% of the log
        # (review B3).
        if (comparable and baseline.get("prev_tool_name") != "Agent"
                and not compacted and tokens_now != baseline["tokens"]):
            entry = logger.build_cost_entry(
                baseline, session_id, transcript_path, tokens_now,
                last_input, last_cache, last_create, last_output,
            )
            logger.append_entry(entry)

    # Extract agent description when the current tool is Agent, so post_tool_use
    # can tag the log entry with agent_type.
    agent_description = ""
    if tool_name == "Agent":
        tool_input = payload.get("tool_input", {})
        if isinstance(tool_input, dict):
            agent_description = tool_input.get("description", "")

    # Save baseline for the next PRE (or Stop hook).
    logger.save_baseline(
        session_id, tokens_now,
        context_tokens=last_input + last_cache + last_output,
        prev_tool_name=tool_name,
        prev_assistant_message=assistant_message,
        turn_number=turn_number,
        task_turn=task_turn,
        user_message=user_message,
        baseline_input=last_input,
        baseline_cache=last_cache,
        baseline_cache_creation=last_create,
        baseline_output=last_output,
        agent_description=agent_description,
    )

    blocks = []

    # Session-length nudge: one-shot suggestion to wrap up when the current
    # prompt size crosses configured thresholds. Skipped for headless runner
    # subprocesses — the suggestion is only actionable in live sessions.
    session_warn_enabled = flags.is_enabled("budgeter-session-warn")
    if session_warn_enabled and os.environ.get("APIARY_RUNNER_SUBPROCESS") != "1":
        # Prompt size = everything the last call read: uncached input, cache
        # reads and cache writes. Leaving cache writes out read a full
        # context as nearly empty on a cache-miss turn (review B5).
        tier, nudge_msg = estimator.session_length_nudge(last_input + last_cache + last_create, config)
        if tier:
            try:
                sid_for_flag = SessionId(session_id)
                flag_file = sid_for_flag.flag_path(f"budgeter_session_len_{tier}_fired")
                if not flag_file.exists():
                    flag_file.parent.mkdir(parents=True, exist_ok=True)
                    flag_file.write_text("1", encoding="utf-8")
                    blocks.append(context_block("budgeter", nudge_msg))
            except ValueError:
                pass

    return HookResult(context=join_contexts(*blocks) or None)


if __name__ == "__main__":
    run_standalone(run)
