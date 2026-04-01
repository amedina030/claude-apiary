# claude-apis Issues

A structured list of issues, drawbacks, and improvements identified in a design review.

---

## Budgeter

### ~~B1 — Warning system compares the wrong signal~~ ✓ FIXED
Replaced TF-IDF entirely. Warning now uses rule-based scope detection on the assistant planning message (4 rules: scope_keywords, breadth_keywords, file_count, step_count; warn at score ≥ 2). Magnitude estimate uses median cost of historically flagged tasks that share at least one rule, with fallback to all flagged tasks. Rules live in config.json. Historical entries backfilled at query time.

### ~~B2 — `[CONT]` instruction injected on every tool call~~ ✓ WONTFIX
Evaluated moving to CLAUDE.md or first-call-only injection. Both reduce reliability of task grouping for negligible token savings (~35 tokens/call, ~3.5% of a session). The repetition is the feature — it guarantees Claude sees the instruction regardless of context compaction. Keeping as-is.

### ~~B3 — `context_tokens` heuristic is questionable~~ ✓ FIXED
Baseline now stores `baseline_input` and `baseline_output` separately. `net_tokens_delta` is computed as `input_growth + last_output` (marginal cost: new context added + new output generated). Old baselines without split fields fall back to the previous heuristic for backward compatibility.

### ~~B4 — Cold start + percentile interplay~~ ✓ FIXED
TF-IDF replaced entirely. Rule-based trigger with `min_flagged_tasks=10` (not 50). Feedback loop added: `feedback.jsonl` records predicted vs actual cost per task. `report.py --feedback` shows per-rule precision breakdown, enabling data-driven threshold and weight tuning via config.json.

---

## Clarifier

### C1 — Ambiguity detection is LLM judgment
The clarifier only fires when Claude decides a request is ambiguous. That detection is inconsistent across model mood, prompt phrasing, and CLAUDE.md load state. No way to audit calibration. Consequential tasks may silently skip clarification; trivial ones may get it.

### C2 — No risk-weighting
"Delete these test files" and "refactor the auth system" go through the same flow. The overhead of spawning a subagent + interactive rounds may exceed the cost of a wrong assumption on low-stakes tasks. No signal for task consequence.

### ~~C3 — Iteration limit fires too late~~ ✓ FIXED
Iteration limit changed from every 5 rounds to every 3 rounds.

### ~~C4 — Hidden dependency on budgeter for session_id~~ ✓ FIXED
The PRE hook now always injects `[budgeter] session_id: <id>` into Claude's context alongside the `[CONT]` instruction. The `log_cost.py finalize` call in CLAUDE.md updated to pass `--session-id` explicitly, eliminating the fragile `budgeter/tmp/` file search.

### ~~C5 — `[CONT]` chaining breaks if budgeter is disabled~~ ✓ FIXED
Root cause was the same as C4 — session_id was `"unknown"` when budgeter-log was off, breaking report attribution. Fixed by the same session_id injection. Note: `[CONT]` chaining in the budgeter log only matters when budgeter-log is on, which is the only time there are entries to chain.

### C6 — CLAUDE.md drift on update
`setup.py` copies clarifier rules to `~/.claude/CLAUDE.md` at install time. Updates to the rules in the repo don't propagate to existing installs without a manual re-run. No version tracking.

---

## Notetaker

### N1 — Thin as a standalone "tool"
Two slash commands that read/write a markdown file. The value is real but marginal. Its standing as a peer to budgeter and clarifier overstates its weight.

### N2 — `/notes` doesn't scale
No filtering by session, date, or keyword. With 20+ notes it becomes a wall of text.

---

## Structural / Cross-cutting

### ~~S1 — Tools don't compose~~ ✓ FIXED
When a budgeter warning fires and the clarifier is enabled, the PRE hook routes through the clarifier for scope review. Cost signal details (triggered rules, estimated magnitude) are passed as context. The clarifier agent now has a fast exit (Step 1b) — if no genuine ambiguity exists, it skips questions and goes straight to final approval, avoiding wasted tokens on clear-but-expensive tasks. When the clarifier is off, the warning falls back to "ask the user."

### S2 — `log_cost.py` is duplicate cost infrastructure *(re-evaluated: not actionable)*
The clarifier pipeline serves a distinct purpose: aggregating multiple Agent calls (each clarifier resume) into one attributed session. Budgeter can't do this natively without a tagging mechanism for individual Agent calls. The pipeline stays. C4/C5 were the real problems here — with session_id now injected reliably, the pipeline is no longer fragile.

### ~~S3 — No install health check~~ ✓ FIXED
`python setup.py --check` validates: hooks registered in settings.json, hook scripts exist, config.json has required keys, data/tmp dirs exist, clarifier files installed, CLAUDE.md rules present, commands installed, flag states shown. Returns exit code 1 on failure.
