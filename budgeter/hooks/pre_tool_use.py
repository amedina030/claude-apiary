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
    "[budgeter] When asking a mid-task clarifying question "
    "(you started executing a task and need user input before continuing), "
    "start your response with [CONT] on its own line."
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

    if flags.is_enabled("budgeter-log"):
        if baseline is not None:
            tokens_delta = max(0, tokens_now - baseline["tokens"])
            context_tokens = baseline.get("context_tokens", 0)
            net_tokens_delta = max(0, tokens_delta - context_tokens)

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "tool_name": baseline.get("prev_tool_name", ""),
                "assistant_message": baseline.get("prev_assistant_message", ""),
                "tokens_delta": tokens_delta,
                "context_tokens": context_tokens,
                "net_tokens_delta": net_tokens_delta,
                "turn_number": baseline.get("turn_number", 0),
                "task_turn": baseline.get("task_turn", baseline.get("turn_number", 0)),
                "project": str(Path(transcript_path).parent) if transcript_path else "",
            }
            logger.append_entry(entry)

    # Save baseline for the next PRE (or Stop hook).
    logger.save_baseline(
        session_id, tokens_now,
        context_tokens=last_input + last_output,
        prev_tool_name=tool_name,
        prev_assistant_message=clean_message,
        turn_number=turn_number,
        task_turn=task_turn,
    )

    # Build context to inject — always include the [CONT] instruction.
    contexts = [_CONT_INSTRUCTION]

    # Warning logic — only fire on the first tool call of a new response.
    warn_enabled = flags.is_enabled("budgeter-warn")
    if warn_enabled and is_new_turn:
        entry_count = logger.count_tasks()
        min_entries = config.get("min_tasks", 50)
        if entry_count >= min_entries:
            log_entries = logger.read_log()
            token_threshold = config.get("expensive_token_threshold") or None
            percentile = config.get("expensive_percentile", 90)

            is_expensive, median_similar, threshold = estimator.estimate(
                clean_message,
                log_entries,
                top_n=config.get("similarity_top_n", 10),
                percentile=percentile,
                token_threshold=token_threshold,
            )

            if is_expensive:
                if token_threshold is not None:
                    threshold_desc = f"hard limit of {threshold:,.0f} tokens"
                else:
                    threshold_desc = f"{percentile}th percentile threshold of {threshold:,.0f} tokens"
                contexts.append(
                    f"[budgeter] Warning: this response is expected to be expensive — "
                    f"similar past responses used ~{median_similar:,.0f} tokens total "
                    f"(your {threshold_desc}). Ask the user if they want to proceed before running."
                )

    hook_allow("\n\n".join(contexts))


if __name__ == "__main__":
    main()
