#!/usr/bin/env python3
"""
PreToolUse hook — logs the previous tool's cost, then optionally warns about the current one.

At PRE time the transcript has already been updated by the API call that processed
the previous tool's result. The delta (tokens_now - baseline.tokens) is therefore
the true cost of the previous tool call, attributed correctly.

The final tool in a session is logged by the Stop hook.
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # claude-apis root

from budgeter.lib import logger, estimator
from core import flags


_CONT_INSTRUCTION = (
    "[budgeter] If you need to ask the user a question mid-task "
    "(you already started executing and need input before continuing), "
    "start your response with [CONT] on its own line. "
    "Never use [CONT] for normal responses or new tasks."
)


def hook_allow(context=None):
    """Allow the tool call, optionally injecting context for Claude."""
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(out))


def _strip_cont(message):
    """Strip a leading [CONT] marker from an assistant message if present."""
    stripped = message.lstrip()
    if stripped.startswith("[CONT]"):
        return stripped[6:].lstrip("\n ")
    return message


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    logger.configure_for_project(cwd)
    config = logger.load_config()

    if tool_name not in config.get("monitored_tools", ["Agent", "Bash"]):
        sys.exit(0)

    session_entries = logger.read_session_jsonl(transcript_path)
    tokens_now = logger.get_cumulative_tokens(session_entries)
    last_input, last_output = logger.get_last_call_tokens(session_entries)
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

    if is_new_turn and is_continuation and baseline is not None:
        task_turn = baseline.get("task_turn", baseline.get("turn_number", turn_number))
    else:
        task_turn = baseline.get("task_turn", turn_number) if baseline is not None else turn_number
        if is_new_turn and not is_continuation:
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
        # a short approval ("yes", "proceed", etc.) and the previous task had flags,
        # inherit them — the approval continues the previous task's risk profile.
        if not scope_flags and baseline is not None:
            prev_flags = baseline.get("scope_flags", [])
            if prev_flags and estimator.is_approval_message(user_message):
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
                "tokens_delta": 1,  # non-zero so it gets written
                "context_tokens": 0,
                "net_tokens_delta": 0,
                "turn_number": turn_number,
                "task_turn": task_turn,
                "scope_flags": scope_flags,
                "project": "",
            })

        if baseline is not None and baseline.get("prev_tool_name") != "Agent" and not compacted:
            tokens_delta = max(0, tokens_now - baseline["tokens"])
            # Marginal cost: input growth (new context added) + new output generated.
            # Falls back to old context_tokens heuristic for baselines without split fields.
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

    # Save baseline for the next PRE (or Stop hook).
    logger.save_baseline(
        session_id, tokens_now,
        context_tokens=last_input + last_output,
        prev_tool_name=tool_name,
        prev_assistant_message=clean_message,
        turn_number=turn_number,
        task_turn=task_turn,
        user_message=user_message,
        scope_flags=scope_flags,
        predicted_cost=predicted_cost,
        warning_fired=warning_fired,
        baseline_input=last_input,
        baseline_output=last_output,
    )

    # Build context to inject — always include the [CONT] instruction and session_id.
    contexts = [_CONT_INSTRUCTION, f"[budgeter] session_id: {session_id}"]

    if warning_fired:
        triggered = ", ".join(scope_flags)
        if median_cost > 0:
            source = "comparable large tasks" if fallback_used else "similar flagged tasks"
            magnitude = f"{source} used ~{median_cost:,.0f} tokens ({sample_size} tasks)"
        else:
            magnitude = "not enough history for a cost estimate yet"

        # Check if the clarifier is enabled — if so, route through it for scope narrowing.
        clarifier_enabled = (Path.home() / ".claude" / "clarifier-enabled").exists()
        if clarifier_enabled:
            contexts.append(
                f"[budgeter] Warning: this response looks potentially expensive — "
                f"triggered: {triggered}. {magnitude}.\n"
                f"The clarifier is enabled. Before proceeding, use the clarifier to narrow scope. "
                f"When spawning the clarifier, include these budgeter details as additional context "
                f"in the detected ambiguities:\n"
                f"  - Cost signal: {triggered}\n"
                f"  - Estimated magnitude: {magnitude}\n"
                f"The clarifier should focus its questions on reducing the scope dimensions "
                f"that triggered this warning (e.g. narrowing which files, which components, "
                f"or whether a full vs targeted approach is needed)."
            )
        else:
            contexts.append(
                f"[budgeter] Warning: this response looks potentially expensive — "
                f"triggered: {triggered}. {magnitude}. "
                f"Ask the user if they want to proceed before running."
            )

    hook_allow("\n\n".join(contexts))


if __name__ == "__main__":
    main()
