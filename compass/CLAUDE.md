# Compass — the rule table

Compass keeps one artifact per target: `<state-dir>/compass/rules.md`, a table of rules written in the second person to Claude ("Prefer the thorough option over the quick one"), scored by this user's own corrections and acceptances (scribe decision D-2026-62). Every session reads it at startup, a compact pin rides on every later user message, and every runner stage gets it as a prompt preamble — so Claude acts the way this user would want at the moments it cannot ask.

Project-level rules live in the repo-root `CLAUDE.md`. This file is only about compass: the pipeline, the lane it occupies, and the rules for working on it.

---

## Pipeline

All state lives under `<state-dir>/compass/` (the per-target dir the registry allocates, `<apiary>/.repos/<name>-<id>/`; path resolution is automatic through the launcher).

1. **Capture** — the Stop hook `core/hooks/compass_pair_log.py` appends `(assistant_text, user_turn, ts)` pairs to `turns/<sid>.jsonl` at the end of every assistant turn (`compass/turns.py`; a cursor file beside it keeps each call proportional to the turn). No model call. Claude Code prunes transcripts, so this has to happen while the session is alive. The same walk scores the turn's final message with the output heuristics (`compass/heuristics.py`) into `events/<sid>.heuristics.jsonl`.
2. **Classify** — `compass/classify.py <sid>` (run by `/wrapup` Step 4; `--catch-up` nightly through the `compass-nightly-classify` entry in `cron_registry/<host>.json`) sends the pairs in **one** batched Sonnet call with a fixed vocabulary — `type` correction | acceptance | anticipation_miss, `section` judgment | output | anticipation, `rule` id or null, `polarity` confirm | contradict, `action`, `quote` — validates the reply against it (out-of-vocabulary items are dropped and counted, never guessed) and writes `events/<sid>.json`. Empty is the expected output for most pairs. A session under 5 pairs is recorded as skipped without a model call.
3. **Build** — `compass/rules.py build --write` counts: seed rows (`compass/seed_rules.json`) + manual rows (`rules_manual.json`) + events -> `rules.md`, with a 60-day half-life, a confidence per row, a flag for specific rows contradicted twice in a row, proposed rows for repeated unattached events, and the heuristics summarised under the Output section (never counted in a row's confidence). Pure function of its inputs; zero events reproduces the seed table; `--check` verifies the file on disk.
4. **Deliver**
   - **Startup**: `core/hooks/startup_prompt_hook.py` injects the whole `rules.md` on the first message of a session (about 1,100 tokens for the seed table).
   - **Pin**: `core/hooks/compass_rules.py` injects the principle rows plus the self-check (about 250 tokens) on every tenth user message, so the rules stay within reach of the turn Claude is composing and survive context compaction. The hook counts the messages itself in a flag file under `session-tmp/`; the model is never asked to keep the count.
   - **Hook points** (the minor path): the same module injects J5 before an `Agent`/`Task` spawn (once per session and agent) and O3 before `AskUserQuestion` (every time).
   - **Runner**: `runner/claude_subprocess.run_claude` prepends `rules.md` to every stage prompt as a `<compass-rules>` block, so a worktree stage with no hook chain still sees it. The classifier opts out (`rules=False`).

`apiary doctor compass` reports the counts (turn sessions, pairs, classified / skipped / pending sessions, events, heuristic turns, `rules.md` rows) and, once 30 sessions are captured, the go/no-go verdict.

---

## Lane discipline — compass vs auto-memory

| Store | What it holds | Authority |
|---|---|---|
| **Auto-memory** (`<state-dir>/scribe/memory/`) | Facts and explicit rules the user stated ("don't mock the DB", "never push without asking") | **Hard** — overrides any compass row |
| **Compass** (`<state-dir>/compass/rules.md`) | Imperatives mined from what the user corrected and accepted, each with a why clause and evidence counts | **Soft** — guidance that decays (60-day half-life) and is scored |

If the user *said* it, it is memory. If the user *did* it — redirected Claude, accepted a recommendation, asked what the reply should have pre-empted — it is compass evidence. A memory entry and a compass row may say the same thing; the memory entry wins, and the row's evidence keeps counting.

---

## Rules for working in this lane

- **Never hand-edit `rules.md`.** It is generated. Add or override rows in `rules_manual.json` (same row schema as `seed_rules.json`; a manual row with a seed id replaces it; `expiry: "YYYY-MM-DD"` marks a temporary constraint). Accept a proposed row by giving it an id there.
- **Row ids are the classifier's vocabulary.** Renaming a seed id orphans its events. Add ids; never recycle them.
- **The row format is a contract.** `rules.render` writes it and `rules.parse_rules_md` reads it back for the pin and the hook-point rules; change them together and keep the round-trip test in `compass/test_rules.py` green.
- **Every row costs tokens in every session.** The seed table is budgeted at about 1,100 tokens (`test_rules.py` pins the size); a row earns its place by changing a decision.
- **Do not write observation files.** `compass/observations/` is read-only history of the retired observation pipeline; nothing reads it.
- **Go/no-go (T-2026-321).** After 30 captured sessions, fewer than about 50 classified events means the capture automation is not earning its keep: `rules.md` then stays a hand-maintained seed table and `turns.py`, `classify.py`, `heuristics.py` and the Stop hook come out. The delivery half stays either way.

---

## Common mistakes to avoid

- **Reading the seed instead of the table.** Delivery code must read `rules.md`; the seed is only an input to the build.
- **Treating the heuristics as evidence.** They are regex rates over Claude's own text, reported for the output rules only; a row's confidence comes from the user's events alone.
- **Classifying a live session.** `--catch-up` leaves turns files younger than two hours alone for a reason; `/wrapup` is the right moment for the current session.
- **Treating compass as authoritative.** It narrows the space of likely preferences; when the stakes are real, ask.
