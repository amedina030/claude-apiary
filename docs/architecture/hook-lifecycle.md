---
type: architecture
title: Hook Lifecycle
scope: budgeter
description: PRE-to-PRE delta pattern, agent special case, and CONT task chaining
framework_version: "1.0"
last_verified: 2026-04-02
---

# Hook Lifecycle

How budgeter measures token cost without spending tokens.

## The PRE-to-PRE delta pattern

Tokens can't be measured during a tool call — only before and after. Budgeter uses the gap between consecutive PreToolUse events:

```
PRE(1): save baseline₁ = current_tokens
  Tool 1 runs
PRE(2): cost₁ = current_tokens - baseline₁    ← logs cost of Tool 1
         save baseline₂ = current_tokens
  Tool 2 runs
PRE(3): cost₂ = current_tokens - baseline₂    ← logs cost of Tool 2
         save baseline₃ = current_tokens
  ...
STOP:   cost_last = current_tokens - baseline_n  ← logs cost of final tool
```

Each PRE hook logs the *previous* tool's cost, then saves a new baseline. The Stop hook captures the last call.

**File:** `budgeter/hooks/pre_tool_use.py`

## Agent calls: the special case

Subagents run in a separate transcript. Their token usage is invisible to the PRE-to-PRE delta — the main transcript's token count doesn't increase by the subagent's consumption.

Instead, Claude Code reports the exact subagent cost in `tool_response.totalTokens` after the Agent call completes. The PostToolUse hook reads this value and logs it directly.

The *next* PreToolUse hook detects that the previous call was an Agent (via a flag in the baseline file) and skips its normal delta logging to avoid double-counting.

**Files:** `budgeter/hooks/post_tool_use.py`, `budgeter/hooks/pre_tool_use.py`

## CONT: task chaining

When Claude asks a mid-task clarifying question and the user replies, subsequent tool calls would be attributed to the user's reply turn — not the original request. This inflates the reply's cost and under-attributes the original task.

To fix this, the hook injects an instruction telling Claude to prefix mid-task questions with `[CONT]`. When the next PRE hook detects `[CONT]` in the assistant message, it inherits `task_turn` from the prior baseline, chaining the continuation back to the originating request.

This affects both the usage report (costs grouped by originating task) and the warning system (scope detection uses the original message, not the reply).

**File:** `budgeter/hooks/pre_tool_use.py` — `_CONT_INSTRUCTION` constant, `_strip_cont()` function

## Baseline files

Each session has a baseline file at `budgeter/tmp/baseline_<session_id>.json` containing:

- `tokens` — token count at last PRE event
- `task_turn` — which user message this tool call chain originates from
- `tool_name` — name of the tool that just ran (for Agent detection)
- `timestamp` — when the baseline was saved

Cleaned up by `budgeter/hooks/stop_session.py` on session end.

## Scope detection and warnings

When warnings are enabled and enough history exists (`min_tasks` in config), the PRE hook evaluates the current assistant message against scope detection rules:

1. **Keyword categories** — investigative terms, file operations, step counts, etc.
2. **Rule weights** — each category has a configurable weight in `config.json`
3. **Weighted score** — if the score exceeds `scope_threshold`, the hook finds similar past tasks
4. **Percentile comparison** — if the median cost of similar tasks exceeds the `expensive_percentile`, a warning is injected

The warning tells Claude the estimated cost and asks the user before proceeding.

**Files:** `budgeter/lib/estimator.py` (rules + scoring), `budgeter/config.json` (weights + thresholds)
