#!/usr/bin/env python3
"""
PreToolUse hook — logs the previous tool's cost, then optionally warns about the current one.

At PRE time the transcript has already been updated by the API call that processed
the previous tool's result. The delta (tokens_now - baseline.tokens) is therefore
the true cost of the previous tool call, attributed correctly.

The final tool in a session is logged by the Stop hook.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apiary root

from budgeter.lib import logger, estimator
from core import flags
from core.hook_context import context_block, join_contexts
from core.hook_context import hook_allow, read_payload  # noqa: F401 — hook_allow used below
from core.session import SessionId


_CONT_INSTRUCTION = context_block(
    "budgeter",
    "If you need to ask the user a question mid-task "
    "(you already started executing and need input before continuing), "
    "start your response with [CONT] on its own line. "
    "Never use [CONT] for normal responses or new tasks.",
)


def _strip_cont(message):
    """Strip a leading [CONT] marker from an assistant message if present."""
    stripped = message.lstrip()
    if stripped.startswith("[CONT]"):
        return stripped[6:].lstrip("\n ")
    return message


def main():
    """Never crash the tool call: any failure is one stderr line and a
    no-objection response (review B1)."""
    try:
        _run()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — hooks must not crash
        print(f"[budgeter] pre_tool_use failed: {exc!r}", file=sys.stderr)
        hook_allow()


def _run():
    payload = read_payload()

    tool_name = payload.get("tool_name", "")
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    logger.configure_for_project(cwd)
    config = logger.load_config()

    if tool_name not in config.get("monitored_tools", ["Agent", "Bash"]):
        hook_allow()
        return

    session_entries = logger.read_session_jsonl(transcript_path)
    tokens_now = logger.get_cumulative_tokens(session_entries)
    last_input, last_cache, last_create, last_output = logger.get_last_call_tokens(session_entries)
    assistant_message = logger.get_last_assistant_message(session_entries)
    turn_number = logger.get_user_turn_number(session_entries)

    # Log the PREVIOUS tool's cost. The baseline was written at the previous PRE,
    # and since then Claude made one API call (to process the previous tool's result
    # and decide to call this tool) — that delta is the previous tool's true cost.
    baseline = logger.load_baseline(session_id)

    # Determine task_turn: inherit from baseline if this turn starts with [CONT],
    # meaning Claude is asking a mid-task clarifying question continuation.
    prev_turn = baseline.get("turn_number", 0) if baseline is not None else 0
    is_new_turn = turn_number > prev_turn
    is_continuation = assistant_message.lstrip().startswith("[CONT]")
    clean_message = _strip_cont(assistant_message)

    if is_new_turn and not is_continuation:
        # A genuinely new task: assign this turn as the task anchor.
        task_turn = turn_number
    elif is_new_turn and is_continuation and baseline is not None:
        # Mid-task clarifying question: inherit the task_turn from baseline.
        task_turn = baseline.get("task_turn", baseline.get("turn_number", turn_number))
    elif baseline is not None:
        # Same turn as before: always use the baseline's task_turn directly.
        # Use "task_turn" key; fall back to "turn_number" only when the baseline
        # predates the task_turn field (old records).  Never fall back to the
        # current turn_number, which would misattribute costs to a different task.
        task_turn = baseline["task_turn"] if "task_turn" in baseline else baseline.get("turn_number", turn_number)
    else:
        task_turn = turn_number

    # Capture user prompt on the first tool call of a new task; inherit it otherwise.
    if is_new_turn and not is_continuation:
        user_message = logger.get_user_message_at_turn(session_entries, turn_number)
    else:
        user_message = baseline.get("user_message", "") if baseline is not None else ""

    # Compute scope_flags on the first tool call of a new non-continuation turn.
    # Always computed (not gated by warn_enabled) so the log has complete data.
    if is_new_turn and not is_continuation:
        scope_flags = estimator.detect_scope_flags(clean_message, config, user_text=user_message)
        # Approval inheritance: if this turn has no flags but the user message is
        # a short approval ("yes", "proceed", etc.) AND the previous task was the
        # immediately preceding turn (no gap), inherit its flags — the approval
        # continues that task's risk profile.
        # Guard: only inherit when the baseline turn is adjacent (prev_turn + 1 ==
        # turn_number) so a "yes" on a wholly new topic doesn't pick up stale flags.
        if not scope_flags and baseline is not None:
            prev_flags = baseline.get("scope_flags", [])
            # Use turn_number (last recorded turn) for adjacency, not task_turn
            # (the task anchor). Multi-turn tasks have prev_turn == turn_number - 1
            # even when task_turn is many turns back, so using task_turn would
            # incorrectly fail the adjacency check for those tasks.
            adjacent = (prev_turn == turn_number - 1)
            if prev_flags and adjacent and estimator.is_approval_message(user_message):
                scope_flags = prev_flags
    else:
        scope_flags = baseline.get("scope_flags", []) if baseline is not None else []

    # Compute warning prediction BEFORE save_baseline so predicted_cost can be stored.
    # This lets the Stop hook / next PRE write accurate feedback records.
    predicted_cost = 0
    warning_fired = False
    median_cost = 0
    sample_size = 0
    fallback_used = False

    warn_enabled = flags.is_enabled("budgeter-warn")
    warn_score_threshold = config.get("warn_score_threshold", 2.0)
    if warn_enabled and is_new_turn and not is_continuation and scope_flags:
        if estimator.score_flags(scope_flags, config) >= warn_score_threshold:
            log_entries_for_warn = logger.read_log()
            median_cost, sample_size, fallback_used = estimator.estimate_magnitude(
                scope_flags, log_entries_for_warn, config
            )
            predicted_cost = int(median_cost)
            warning_fired = True

    compacted = (baseline is not None
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
                "scope_flags": scope_flags,
                "project": "",
                "_marker": True,  # force-write despite zero deltas
            })

        # No API call happened since the last PRE (parallel tool calls in one
        # assistant turn): there is no cost to attribute, so log nothing.
        # Logging last_output here created phantom entries — 25% of the log
        # (review B3).
        if (baseline is not None and baseline.get("prev_tool_name") != "Agent"
                and not compacted and tokens_now != baseline["tokens"]):
            entry = logger.build_cost_entry(
                baseline, session_id, transcript_path, tokens_now,
                last_input, last_cache, last_create, last_output,
            )
            logger.append_entry(entry)

        # Write feedback for the previous task at task-completion boundary.
        # actual_cost is computed at report time by joining against the log,
        # so only the prediction metadata needs to be stored here.
        if baseline is not None and is_new_turn and not is_continuation:
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
        prev_assistant_message=clean_message,
        turn_number=turn_number,
        task_turn=task_turn,
        user_message=user_message,
        scope_flags=scope_flags,
        predicted_cost=predicted_cost,
        warning_fired=warning_fired,
        baseline_input=last_input,
        baseline_cache=last_cache,
        baseline_cache_creation=last_create,
        baseline_output=last_output,
        agent_description=agent_description,
    )

    # Build context to inject. The [CONT] instruction and session_id only
    # need to be injected once per session — they stay in context for the
    # rest of the conversation. Without this guard they re-fire on every
    # tool call and clutter the transcript (T-2026-117).
    blocks = []
    try:
        sid = SessionId(session_id)
        flag_file = sid.flag_path("budgeter_context_done")
        if not flag_file.exists():
            flag_file.parent.mkdir(parents=True, exist_ok=True)
            flag_file.write_text("1", encoding="utf-8")
            blocks.append(_CONT_INSTRUCTION)
            blocks.append(context_block("budgeter", f"session_id: {session_id}"))
    except ValueError:
        pass

    if warning_fired:
        triggered = ", ".join(scope_flags)
        if median_cost > 0:
            source = "comparable large tasks" if fallback_used else "similar flagged tasks"
            magnitude = f"{source} used ~{median_cost:,.0f} tokens ({sample_size} tasks)"
        else:
            magnitude = "not enough history for a cost estimate yet"

        blocks.append(context_block(
            "budgeter",
            f"Warning: this response looks potentially expensive — "
            f"triggered: {triggered}. {magnitude}. "
            f"Ask the user if they want to proceed before running.",
        ))

    # Session-length nudge: one-shot suggestion to wrap up when the current
    # prompt size crosses configured thresholds. Skipped for headless runner
    # subprocesses — the suggestion is only actionable in live sessions.
    # Gated separately from budgeter-warn so users can opt into the context-fill
    # nudge without also re-enabling the noisier magnitude warning.
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

    hook_allow(join_contexts(*blocks))


if __name__ == "__main__":
    main()
