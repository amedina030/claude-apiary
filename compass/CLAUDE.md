# Compass — Personality Profile and Behavioral Read

Compass captures personality and behavior signals from sessions and synthesizes them into a profile (`personality.md`) that future sessions read at startup. The goal is to let Claude anticipate this user's preferences and act in alignment with them — especially in headless/runner sessions where Claude can't ask.

Project-level rules live in the repo-root `CLAUDE.md`. This file is only about compass: the lane it occupies, when to write observations, what makes a good observation, and how the synthesis cycle works.

---

## Transition: the rule table (D-2026-62)

Compass is being replaced by a **second-person rule table**, `<state-dir>/compass/rules.md`, scored by the user's own corrections and acceptances instead of by a model's inference about the user. Step 1 (T-2026-319) shipped the capture side; step 2 (T-2026-320) replaces delivery and retires the observation pipeline below. Until step 2 lands, `personality.md` is still what the startup hook injects, so both halves of this file are live.

The new pipeline, all under `<state-dir>/compass/`:

1. **Stop hook** `core/hooks/compass_pair_log.py` appends `(assistant_text, user_turn, ts)` pairs to `turns/<sid>.jsonl` every turn (no model call; cursor file keeps it cheap). Claude Code prunes transcripts, so capture happens while the session is alive.
2. **`compass/classify.py <sid>`** (called by `/wrapup` Step 4; `--catch-up` nightly) sends the pairs in one batched Sonnet call with a fixed vocabulary — `type` correction | acceptance | anticipation_miss, `section` judgment | output | anticipation, `rule` id or null, `polarity` confirm | contradict, `action`, `quote` — validates the reply, writes `events/<sid>.json`. Empty is the expected output for most pairs.
3. **`compass/rules.py build --write`** counts: seed rows (`compass/seed_rules.json`) + manual rows (`rules_manual.json`) + events -> `rules.md`, with 60-day half-life decay, a confidence per row, a flag for specific rows contradicted twice in a row, and proposed rows for repeated unattached events. Pure function; zero events reproduces the seed table.

Rules for working in this lane now:

- **Never hand-edit `rules.md`.** Add or override rows in `rules_manual.json`; accept a proposed row by giving it an id there.
- **Do not add observation files** (`/wrapup` no longer does). `compass/observations/` is read-only history.
- **Row ids are the classifier's vocabulary.** Renaming a seed id orphans its events.
- **Go/no-go**: after 30 captured sessions, fewer than ~50 classified events means the pipeline is not earning its keep; `rules.md` then stays a hand-maintained seed table and the capture code goes. `apiary doctor compass` reports the counts.

Everything below this line describes the observation pipeline that step 2 retires.

---

## Lane discipline — compass vs auto-memory

Compass and auto-memory are deliberately separate stores:

Both stores live under the per-target state directory the registry allocates (`<apiary>/.repos/<name>-<id>/` post-C-2026-46) — `<state-dir>/scribe/memory/` for auto-memory and `<state-dir>/compass/` for compass. Path resolution is automatic when invoking the tools via the launcher.

| Store | What it captures | Lifespan | Authority |
|---|---|---|---|
| **Auto-memory** (`<state-dir>/scribe/memory/`) | Facts, rules, explicit user statements ("don't mock the DB", "I'm a senior Go engineer") | Permanent, manually edited | **Hard** — explicit user statements override compass |
| **Compass** (`<state-dir>/compass/`) | Personality, behavior patterns, tone, decision style — *how* the user engages | Decays; rolling 50-session window with archive | **Soft** — guidance for the LLM, not a binding rule |

If something the user said reads more like a *rule* ("always use list-form subprocess") or a *fact* ("I work primarily in Go"), it belongs in auto-memory, not compass. If it reads more like a *trait* ("user pushes back on suggestions with 'why X not Y' rather than direct rejection"), that's compass.

When in doubt: would the same fact apply to anyone with the same role/setup? → auto-memory. Is it about *this specific person's style*? → compass.

---

## When observations are written

Three paths produce per-session observation files (`compass/observations/<session_id_short>.json`):

1. **`/wrapup` capture** (primary). Step 4 of `/wrapup` extracts 3–7 observations inline from the current session. Non-blocking — wrapup completes even if capture fails.
2. **`compass/backfill.py`** (manual). Operator-invoked CLI that processes historical transcripts via headless Claude. Selectors: `--last N`, `--session-ids X,Y,Z`, `--since YYYY-MM-DD`.
3. **No automatic hook.** Capture is deliberately not a `Stop` hook — putting it in `/wrapup` keeps it visible, debuggable, and skippable when the session was startup-only.

---

## Observation quality bar

A good observation:
- Names a **dimension** from `compass/dimensions.json` (currently 9 — communication_style, decision_making, pushback, engagement, autonomy, risk_tolerance, trust_calibration, meta_awareness, mood_tone).
- Describes the trait/pattern in 1–2 sentences.
- Includes **evidence** — a short quote or paraphrase from the actual session that supports the claim.
- Has a **volatility** tag: `stable` for personality traits, `volatile` for current-mood/state observations.

A bad observation:
- Pads to hit 3–7 when the signal isn't there. **Empty observations is fine — fabrication is harmful.**
- States facts the user explicitly said (those go in auto-memory).
- Cites no specific evidence.
- Mixes dimensions ("user is decisive AND terse" — split into two observations).
- Generalizes from a single moment without explanation (a one-off pushback isn't a personality trait — note it as `volatile` if at all).

When extracting observations during `/wrapup`, **err on the side of fewer high-quality observations**. The synthesizer aggregates across sessions; one solid observation per session beats five weak ones.

---

## Synthesis cycle

`compass/synthesize.py` reads active observations + previous `personality.md` + `corrections.md` and asks headless `claude -p` to produce a new `personality.md`. Two trigger paths:

- **Weekly cron**: `cron_registry/<hostname>.json` (one file per machine) has `compass-weekly-synthesis` running daily at 03:00 with `--cron`, which self-throttles to 7 days (skips if `personality.md` was updated within the last week).
- **Manual**: `/compass-sync` slash command for "I had a big shift, sync now."

The Windows Task Scheduler backend only supports `daily`, which is why the daily-with-throttle pattern is used instead of a true weekly schedule.

---

## Conflict handling

When observations disagree:
- More recent observations (by `captured_at`) win — the synthesizer is told to weigh them higher.
- The synthesizer notes evolution explicitly when a clear shift has occurred ("Earlier sessions showed X; recent sessions show Y").
- For volatile dimensions (`mood_tone`), only the last 5 sessions count toward synthesis — single old mood signals don't ossify into permanent traits.

When auto-memory and compass disagree:
- **Auto-memory feedback wins as a hard rule, compass is soft personality flavor.** Both are injected at startup; the model treats explicit feedback as binding and compass as guidance.
- True conflicts should be rare if the lanes stay clean. Most apparent conflicts are stale auto-memory entries — fix those by updating the memory file, not by adjusting compass.

---

## `corrections.md` — manual override path

`<state-dir>/compass/corrections.md` is a free-text file the user (or Claude on the user's behalf) edits when the synthesizer gets something wrong. The synthesizer treats its content as **high-weight evidence** that overrides raw observations.

Use corrections when:
- The synthesized profile mischaracterizes a trait and you want it fixed *now* without waiting for new observations to accumulate.
- A trait is changing (e.g. user is moving from "wants check-ins" to "wants more autonomy") and you want the synthesizer to weight that shift heavily.

Don't use corrections to encode rules — those go in auto-memory feedback.

---

## Bloat handling

- **Active observations** stay in `observations/<sid>.json` until they exceed the rolling window.
- **Archive** moves files older than 90 days into `observations/archive/<iso-year>-<iso-week>/`, **but only when active count is ≥ 50**. Below that, no archive runs (preserves data while building signal).
- **`personality.md` doesn't grow** — each synthesis rewrites it. Bounded by design.

Archive sweep: `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/observations.py archive [--apply]` (dry-run by default).

---

## Measurement — compass is an experiment, and it is being scored

Compass is kept on the condition that it is measured (review §5a-H). Three
instruments exist; the full design, the metric definition, the honesty
caveats and the **proposed keep/delete rule** live in
`docs/architecture/compass-measurement.md`.

- **`compass/evaluate.py offline`** — leave-one-out over the observation
  files: does a profile synthesized from the *other* sessions predict a
  held-out session's per-dimension labels? Reports accuracy against a
  majority and a random baseline. **Lift over majority is the number that
  matters**, and this measures the profile's *internal consistency*, not
  whether injecting it changes behaviour. Default run uses a deterministic
  stub and calls no model; `--model` costs one `claude -p` per fold and
  needs `--yes`.
- **`compass/evaluate.py ab`** — the real test. Each session is assigned to
  arm `on` (profile injected) or `off` (not), recorded as `compass_arm` in
  its identity file, and the arms are compared against budgeter outcome
  proxies. **Off by default** (`compass/config.json` `ab_enabled: false`), so
  today every session is in arm `on` and nothing has changed.
- **`apiary doctor compass`** — observation counts, synthesis age (warns
  above 14 days), profile size, arm counts, last headline. Report-only.

When touching this subsystem: `label_vocabulary.json` and `config.json` are
part of a live experiment. Editing the vocabulary changes the metric; editing
`ab_seed` re-rolls the split. Neither is a routine edit.

---

## Common mistakes to avoid

- **Adding a new dimension casually.** Edit `dimensions.json` deliberately — the synthesizer only emits sections for configured dimensions, and ad-hoc dimensions in observations get rejected by the validator.
- **Trusting the LLM to echo back templated `session_id` in observation JSON.** It can latch onto commit hashes or other hex strings in the transcript. `backfill.py` overrides `session_id` after extraction; `/wrapup` capture is in-context (lower risk) but the `validate` CLI's filename check catches drift.
- **Writing observation files manually.** Always go through `/wrapup`, `backfill.py`, or hand-edit `corrections.md`. Don't synthesize observation files yourself — they're meant to be extracted from real session signal.
- **Treating compass as authoritative.** It's *guidance*. When in doubt, ask the user. Compass narrows the space of likely preferences; it doesn't eliminate the need to check on important decisions.
