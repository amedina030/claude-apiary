---
type: architecture
title: Hook Lifecycle
scope: budgeter
description: PRE-to-PRE delta pattern, the Agent special case, task attribution, baselines, and the session-length and usage-limit nudges
framework_version: "1.0"
last_verified: "2026-09-11"
---

# Hook Lifecycle

How budgeter measures token cost without spending tokens.

**Where these run.** Budgeter's three hooks are rows in the dispatcher's
registry, not settings.json entries of their own: `core/hooks/dispatch.py`
reads the payload once per event and calls each hook's `run(payload)`
in-process, matching `budgeter/config.json`'s `monitored_tools` against the
tool name before importing anything. The registry, the ordering and the
failure log are in [Hooks](../reference/hooks.md); this document is only about
what the budgeter hooks *do* once they are called.

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

## Usage-limit samples

The same Stop hook records one usage-limit sample after it has logged the
final call: it fetches the Claude Code OAuth usage endpoint through
`core/usage_fetcher.py` and appends the 5-hour and 7-day utilization (with
their reset times and a `hook` source tag) to
`budgeter/data/usage_samples.jsonl`, but only when the last sample there is
older than `usage_sample_interval_seconds` (`budgeter/config.json`, default
300). The GUI's poller appends the payload it already holds under the same
gate with a `gui` tag. The file is always the main apiary copy: usage is per
account, so the per-project redirect the token log honours does not apply.

The sample is what the token log is not, the quantity the subscription's
limits are actually drawn down by. `budgeter/usage_calibrate.py` joins it to
the transcript load `budgeter/bill.py` computes. On by default in every repo,
silenced per repo by the `budgeter-usage-sample-off` flag; fail-open, bounded
by the fetcher's 5-second timeout, and refused outright under
`APIARY_BUDGETER_TEST_ISOLATION=1` before any network call.

## Warnings

The two nudges below are the whole of it. Both warn about a ceiling that has
been measured, never about a prediction. The rule-based "this task looks
expensive" warning was measured at 9% precision over 3,717 tasks against a
25% base rate and deleted in the 2026-08 review, along with
`budgeter/tune.py`, the feedback log and the `budgeter-warn` flag.

## Session-length nudge

Gated by the `budgeter-session-warn` flag. On each PRE the hook compares the
size of the last prompt (uncached input + cache reads + cache writes) against
`session_warn_soft_tokens` and `session_warn_hard_tokens` from the config. The
first crossing of each tier injects one advisory and stamps a per-session flag
file so it never repeats. Skipped entirely when `APIARY_RUNNER_SUBPROCESS=1` —
the suggestion is only actionable in a live session.

**Files:** `budgeter/lib/estimator.py` (`session_length_nudge`),
`budgeter/config.json` (thresholds)

## Usage-limit nudge

The session-length nudge measures how full the context window is. This one
measures how much of the subscription is left, which is the other ceiling a
long session runs into.

On each PRE the hook reads the newest line of
`budgeter/data/usage_samples.jsonl` and compares the `five_hour` and
`seven_day` utilizations against `usage_warn_<window>_soft_pct` and
`usage_warn_<window>_hard_pct`. Each window warns independently, so a spent
7-day limit still surfaces during a fresh 5-hour one. The model-specific
`seven_day_opus` and `seven_day_sonnet` sub-meters are deliberately not
covered: they move with whichever model is in use, and a warning about a
meter you are not drawing down is noise.

It reads the sample file rather than calling the usage endpoint. A fetch
carries the fetcher's 5-second timeout, and paying that on the path of every
monitored tool call is not a trade worth making. The Stop hook already
refreshes the file at the end of every turn, so the cost is only that within
one long turn the number is frozen at the turn's start.

Reading a file instead of fetching means the sample can describe a world that
no longer exists, so two staleness guards apply. A sample older than
`usage_warn_max_sample_age_seconds` is ignored outright. A window whose
`resets_at` has already passed is skipped individually, because its
utilization is the previous window's high-water mark and would otherwise
produce a confident, wrong warning.

Unlike the session-length nudge this is **on by default** in every repo. The
limits are per account, so a per-repo opt-in would leave most repos silent
about a ceiling that stops work everywhere. The `budgeter-usage-warn-off`
flag silences it per repo, matching the sampler's kill switch.
`APIARY_RUNNER_SUBPROCESS=1` skips it for the same reason the session nudge
does: there is nobody there to act on it.

The per-session sentinel stores the `resets_at` it fired for, so a sample
carrying a new reset time re-arms the nudge. A session that outlives its
5-hour window therefore warns again in the next one.

**Files:** `budgeter/lib/estimator.py` (`usage_limit_nudge`),
`budgeter/lib/usage_samples.py` (`latest_sample`), `budgeter/config.json`
(thresholds)
