---
type: architecture
title: Hook Lifecycle
scope: budgeter
description: PRE-to-PRE delta pattern, the Agent special case, task attribution, baselines and the session-length nudge
framework_version: "1.0"
last_verified: 2026-08-26
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

## Task attribution

Every log entry carries a `task_turn`: the user turn that started the task it
belongs to. The first monitored tool call of a new user turn opens a new task;
later calls in the same turn inherit the anchor from the baseline.

A `[CONT]` marker used to chain a mid-task clarifying question back to the
originating turn. It could almost never fire — the Stop hook deletes the
baseline at the end of *every* assistant turn, so the next turn starts with no
baseline to inherit from — and the instruction telling Claude to emit the
marker cost context in every session. Both were deleted in the 2026-08 review
(finding B7).

**File:** `budgeter/hooks/pre_tool_use.py`

## Baseline files

Each session has a baseline file at `budgeter/tmp/<session_id>_baseline.json`
(or `<project>/.claude/budgeter-tmp/` when the project has a
`.claude/budgeter.json`) containing:

- `schema` — bumped when the meaning of the numbers changes; a baseline from
  an older schema is kept for turn continuity but never subtracted from
- `tokens` — cumulative token count at the last PRE, deduped per API call
- `baseline_input` / `baseline_cache` / `baseline_cache_creation` /
  `baseline_output` — the split of the last API call, for the marginal-cost figure
- `turn_number` / `task_turn` — the current user turn, and the task it belongs to
- `prev_tool_name` — the tool that just ran (also how the Agent double-count guard works)
- `prev_assistant_message` / `user_message` / `agent_description` — context carried onto the entry

Written atomically (temp file + `os.replace`), and deleted by
`budgeter/hooks/stop_session.py` at the end of every assistant turn — the Stop
hook fires per response, not per session.

## Warnings

There are none any more, beyond the session-length nudge below. The
rule-based "this task looks expensive" warning was measured at 9% precision
over 3,717 tasks against a 25% base rate and deleted in the 2026-08 review,
along with `budgeter/tune.py`, the feedback log and the `budgeter-warn` flag.

## Session-length nudge

Gated by the `budgeter-session-warn` flag. On each PRE the hook compares the
size of the last prompt (uncached input + cache reads + cache writes) against
`session_warn_soft_tokens` and `session_warn_hard_tokens` from the config. The
first crossing of each tier injects one advisory and stamps a per-session flag
file so it never repeats. Skipped entirely when `APIARY_RUNNER_SUBPROCESS=1` —
the suggestion is only actionable in a live session.

**Files:** `budgeter/lib/estimator.py` (`session_length_nudge`),
`budgeter/config.json` (thresholds)
