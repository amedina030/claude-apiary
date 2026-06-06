# Compass — Personality Profile and Behavioral Read

Compass captures personality and behavior signals from sessions and synthesizes them into a profile (`personality.md`) that future sessions read at startup. The goal is to let Claude anticipate this user's preferences and act in alignment with them — especially in headless/runner sessions where Claude can't ask.

Project-level rules live in the repo-root `CLAUDE.md`. This file is only about compass: the lane it occupies, when to write observations, what makes a good observation, and how the synthesis cycle works.

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

- **Weekly cron**: `runner/cron_registry.json` has `compass-weekly-synthesis` running daily at 03:00 with `--cron`, which self-throttles to 7 days (skips if `personality.md` was updated within the last week).
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

## Common mistakes to avoid

- **Adding a new dimension casually.** Edit `dimensions.json` deliberately — the synthesizer only emits sections for configured dimensions, and ad-hoc dimensions in observations get rejected by the validator.
- **Trusting the LLM to echo back templated `session_id` in observation JSON.** It can latch onto commit hashes or other hex strings in the transcript. `backfill.py` overrides `session_id` after extraction; `/wrapup` capture is in-context (lower risk) but the `validate` CLI's filename check catches drift.
- **Writing observation files manually.** Always go through `/wrapup`, `backfill.py`, or hand-edit `corrections.md`. Don't synthesize observation files yourself — they're meant to be extracted from real session signal.
- **Treating compass as authoritative.** It's *guidance*. When in doubt, ask the user. Compass narrows the space of likely preferences; it doesn't eliminate the need to check on important decisions.
