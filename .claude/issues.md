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

### ~~C1 — Ambiguity detection is LLM judgment~~ ✓ FIXED
Non-trivial tasks now always spawn the clarifier — LLM ambiguity judgment is no longer a gate. The clarifier itself decides whether there's ambiguity: if none, it fast-exits silently (no user prompt) and returns the original prompt unchanged. Trivial tasks still use LLM judgment, which is acceptable given their low stakes.

### ~~C2 — No risk-weighting~~ ✓ FIXED
Addressed by the combination of: (1) S1 — budgeter routes expensive tasks to the clarifier with cost context, (2) C1 fix — all non-trivial tasks go through clarifier, but clear tasks fast-exit silently with no user interaction, (3) trivial tasks skip clarifier entirely. The overhead for clear non-trivial tasks is one silent subagent round-trip (~2-3k tokens).

### ~~C3 — Iteration limit fires too late~~ ✓ FIXED
Iteration limit changed from every 5 rounds to every 3 rounds.

### ~~C4 — Hidden dependency on budgeter for session_id~~ ✓ FIXED
The PRE hook now always injects `[budgeter] session_id: <id>` into Claude's context alongside the `[CONT]` instruction. The `log_cost.py finalize` call in CLAUDE.md updated to pass `--session-id` explicitly, eliminating the fragile `budgeter/tmp/` file search.

### ~~C5 — `[CONT]` chaining breaks if budgeter is disabled~~ ✓ FIXED
Root cause was the same as C4 — session_id was `"unknown"` when budgeter-log was off, breaking report attribution. Fixed by the same session_id injection. Note: `[CONT]` chaining in the budgeter log only matters when budgeter-log is on, which is the only time there are entries to chain.

### ~~C6 — CLAUDE.md drift on update~~ ✓ FIXED
`setup.py --global` now writes `.install-manifest.json` with SHA-256 hashes of all installed files. `setup.py --check` compares installed files against the manifest and reports drift. A `core/hooks/check_install.py` PreToolUse hook runs once per session and warns if any files are stale. Drift still requires a manual `setup.py --global` to fix, but it's now detected automatically.

---

## Notetaker

### ~~N1 — Thin as a standalone "tool"~~ ✓ FIXED
Notetaker replaced by Scribe — a full Python-backed note management system. JSONL storage, 7 note types (todo, handoff, decision, wishlist, reference, blocker, context), structured CLI with add/list/get/done/update/archive/migrate. Auto-load hook injects notes at session start. Stop hook saves stripped transcript for handoff generation. CLAUDE.md rules define when to auto-write notes.

### ~~N2 — `/notes` doesn't scale~~ ✓ FIXED
`notes.py list` returns compact one-liners with Python-computed age. Filtering by type, session, keyword, and recency. Auto-archives done notes and old handoffs after 30 days. Archive searchable via `--archive` flag.

---

## Structural / Cross-cutting

### ~~S1 — Tools don't compose~~ ✓ FIXED
When a budgeter warning fires and the clarifier is enabled, the PRE hook routes through the clarifier for scope review. Cost signal details (triggered rules, estimated magnitude) are passed as context. The clarifier agent now has a fast exit (Step 1b) — if no genuine ambiguity exists, it skips questions and goes straight to final approval, avoiding wasted tokens on clear-but-expensive tasks. When the clarifier is off, the warning falls back to "ask the user."

### S2 — `log_cost.py` is duplicate cost infrastructure *(re-evaluated: not actionable)*
The clarifier pipeline serves a distinct purpose: aggregating multiple Agent calls (each clarifier resume) into one attributed session. Budgeter can't do this natively without a tagging mechanism for individual Agent calls. The pipeline stays. C4/C5 were the real problems here — with session_id now injected reliably, the pipeline is no longer fragile.

### ~~S3 — No install health check~~ ✓ FIXED
`python setup.py --check` validates: hooks registered in settings.json, hook scripts exist, config.json has required keys, data/tmp dirs exist, clarifier files installed, CLAUDE.md rules present, commands installed, flag states shown. Returns exit code 1 on failure.
