---
type: architecture
title: "Compass rule table"
scope: project
description: How the second-person rule table is captured, classified, built, delivered and measured, and the go/no-go on its automation
framework_version: "1.0"
last_verified: 2026-09-06
---

# Compass rule table

Compass is the answer to one question: **act the way this user would want at
the moments Claude cannot ask** — in interactive sessions while composing a
recommendation, and in headless runner stages where nobody is there to ask.

Until 2026-09 it answered that with a descriptive personality profile
synthesised weekly from model-written observations, plus a measurement
programme (an offline metric and a live A/B) that was never run. Scribe
decision **D-2026-62** (2026-09-06) replaced all of it with a single artifact:
`<state-dir>/compass/rules.md`, a table of imperatives written in the second
person to Claude and scored by the user's own corrections and acceptances.
Step 1 (T-2026-319) shipped the capture side; step 2 (T-2026-320) shipped
delivery and retired the profile. This document describes what exists now.

---

## 1. The artifact

`rules.md` has three sections, each holding **principle** rows (general,
trait-level, imperative) and **specific** rows that cite the principle they
instantiate:

| Section | Question it answers | Seed principles |
|---|---|---|
| Judgment | what to decide | J1 prefer the thorough option; J2 keep going inside an agreed plan; J3 stop when a premise weakens |
| Output | how to write | O1 lead with the outcome and one recommendation; O2 full written English |
| Anticipation | what to pre-empt | A1 answer "why not the simpler thing" first; A2 expect "is this it?"; A3 expect the cost audit |

Each row carries an id, the rule, a **why** clause (the rationale, which also
says where the rule stops applying), source and confidence on the header
line, and — once events exist — an evidence line with the confirmed and
contradicted counts, the last-seen date and one verbatim quote. The file ends
with a five-item **self-check** applied before finalizing any recommendation
or report. `compass/seed_rules.json` is the shipped seed; the current table
for this machine is the file itself.

A row's header looks like

```
- **J5** (specific, J2; mined 0.75) Treat usage-limit draw-down as a first-class cost in every proposal; never fan out agents without naming the cost.
  - why: subscription only; "don't fan out, use usage conservatively"
  - evidence: 1 confirmed, 0 contradicted, last 2026-09-06; "syntehtsis can be done in sonnet"
```

The format is a contract: `compass/rules.py` renders it and reads it back
(`parse_rules_md`) for the deliveries below.

---

## 2. Signal

The one hard fact in a transcript is "Claude did A, the user redirected to
B". Three event types are mined from `(assistant text, user turn)` **pairs**,
never from a user turn alone:

| Type | Shape | Polarity |
|---|---|---|
| `correction` | Claude did A, the user redirected to B (includes an interrupt then a redirect, and a transparency-miss question) | `contradict` when the user steered away from what a rule prescribes; `confirm` when the correction was *towards* the rule |
| `acceptance` | Claude proposed or did A and the user said go ("yep", "next", "your rec") | `confirm` |
| `anticipation_miss` | the user asked what the previous reply should have pre-empted ("why not X", "how do I check", "is this it?") | either |

`polarity` is about the rule as a description of what the user wants; the
event type carries whether Claude complied. Empty output is the expected
result for most pairs, and the classifier is told so.

**Capture is per session and happens while the session is alive**, because
Claude Code prunes transcripts (L-2026-180): the Stop hook
`core/hooks/compass_pair_log.py` appends pairs to `turns/<sid>.jsonl` at the
end of every assistant turn through `compass/turns.py`, whose byte-offset
cursor makes each call proportional to the turn. The record filter keeps only
real user prompts (`promptId`, string content, no attachment, not a
sidechain, not `entrypoint == "sdk-cli"`, not a task notification or a
slash-command invocation).

**Classification** is `compass/classify.py <sid>`: one batched Sonnet
`claude -p` call per session (prompt via stdin, `max_turns=1`, no tools, no
rules preamble), reply validated against the fixed vocabulary — anything
outside it is dropped and counted, never guessed — written to
`events/<sid>.json`. `/wrapup` Step 4 runs it for the current session; the
`compass-nightly-classify` cron entry (03:30, `--catch-up`) sweeps finished
sessions that ended without a wrapup. Sessions under 5 pairs are recorded as
skipped with no model call. The first real classification (6 pairs) took
about 205 s and produced 7 events.

---

## 3. Aggregation (no model)

`compass/rules.py build` is a pure function of the seed, the manual rows,
the events and `now`:

* each event weighs `0.5 ** (age_days / 60)` (60-day half-life);
* per rule: confirmed and contradicted weight, raw counts, last seen, one
  quote, and `confidence = (confirmed + 0.5) / (confirmed + contradicted + 1)`
  — 0.50 with no evidence;
* events on a specific row also count on its parent principle;
* a **specific** row whose last two events both contradict it is **flagged**
  in the table, not demoted;
* three or more unattached events sharing `(section, action)` are listed as a
  **proposed** row, accepted by giving it an id in `rules_manual.json`;
* a manual row with a seed id replaces it; `expiry: "YYYY-MM-DD"` marks a
  temporary constraint ("usage is tight this week") that drops on its own.

With zero events the build reproduces the seed table byte for byte;
`build --check` verifies the on-disk file against a fresh build.

---

## 4. Delivery

Most of what the rules govern happens while Claude composes, which no hook
can see, so the table is **always in context** and hook-point injection is
the minor path.

| Path | Mechanism | Size | When |
|---|---|---|---|
| Startup | `core/hooks/startup_prompt_hook.py` injects the whole `rules.md` | about 1,100 tokens (seed) | first user message of a session |
| Pin | `core/hooks/compass_rules.py` injects the principle rows plus the self-check; the hook counts messages in a flag file, never the model | about 250 tokens | every tenth user message |
| Hook points | the same module injects J5 before `Agent`/`Task` (once per session and agent) and O3 before `AskUserQuestion` (every time) | one row | PreToolUse |
| Runner | `runner/claude_subprocess.run_claude` prepends `rules.md` as a `<compass-rules>` block | the whole table | every stage prompt: refine, plan, execute, verify, harden, approval |

The pin exists so the rules survive context compaction and stay within a
few turns of the one being composed; every tenth message was chosen on
2026-09-06 as the starting cadence (every message was the original spec and
cost about 250 tokens per turn) and may be revisited. The runner path is a
prompt preamble rather than a
hook on purpose: a runner worktree carries no `.claude/settings.json`, so no
hook chain fires there (T-2026-318), and the preamble works everywhere a
stage runs. All deliveries read the rendered `rules.md`, so a manual override
changes what Claude sees and a target with no table gets nothing.

---

## 5. Measurement

Two signals, one instrument.

* **Correction rate per section per session over time**, from the event
  stream itself. Judgment and anticipation rules are scored by the user's
  events only.
* **Output heuristics** (`compass/heuristics.py`), a model-free secondary
  signal for the output rules: at every Stop, the turn's final assistant
  message is scored for *outcome in the first sentence* (not a question, not
  a process opener such as "I'll" / "Let me" / "Now", at most 40 words), *at
  most one recommendation* (recommendation markers ≤ 1, no menu of
  alternatives) and a *length band* (150–3,000 chars). Rows go to
  `events/<sid>.heuristics.jsonl` with `source: heuristic`; the build
  summarises the rates of every classified session under the Output section
  and **never** counts them in a row's confidence. They are regexes over
  free text: read them as a trend, not a measurement.

`apiary doctor compass` is the instrument: turn sessions, pairs, classified
/ skipped / pending sessions, events, heuristic turns, `rules.md` rows
(flagged, proposed, age) and the go/no-go below. Report-only — notes, never
issues.

---

## 6. Cost

| Item | Cost |
|---|---|
| Startup block | about 1,100 input tokens once per session, cached prefix thereafter |
| Pin | about 250 input tokens every tenth user message |
| Runner preamble | about 1,100 input tokens per stage call (a night with 30 stage calls is roughly 35k tokens) |
| Classification | one Sonnet call per wrapped-up session, about 3 minutes wall clock, no in-context work |
| Stop hook | file I/O only |

What went away: the weekly Opus synthesis (about 38k input tokens per run)
and the 3–7 observations `/wrapup` wrote inline every session.

---

## 7. Go/no-go on the automation (T-2026-321)

The seed table already beats the profile it replaced, so it ships
regardless. The **capture automation** — the Stop hook, `turns.py`,
`classify.py`, `heuristics.py`, the nightly cron entry — has to earn its keep:

* **After 30 captured sessions** (`apiary doctor compass` prints the verdict
  once the threshold is reached), **keep** the automation only if at least
  **50 classified events** exist by then.
* Otherwise `rules.md` stays a hand-maintained seed table (rows added through
  `rules_manual.json`) and the capture code is removed. Delivery stays.

Record the decision as a scribe decision note either way.

---

## 8. Files

| Path | What it is |
|---|---|
| `compass/seed_rules.json` | The shipped seed table (sections, rows, self-check) |
| `compass/turns.py` | Pair extraction and the incremental cursor |
| `compass/heuristics.py` | The output heuristics and their storage |
| `compass/classify.py` | The Sonnet classifier and `--catch-up` |
| `compass/rules.py` | Aggregation, rendering, and the parse / pin / rule-line readers |
| `compass/health.py` | The facts behind `apiary doctor compass` |
| `core/hooks/compass_pair_log.py` | Stop hook: capture |
| `core/hooks/compass_rules.py` | UserPromptSubmit pin and PreToolUse hook-point rules |
| `runner/claude_subprocess.py` | `rules_preamble` for stage prompts |
| `<state-dir>/compass/turns/<sid>.jsonl`, `.cursor.json` | Captured pairs and the cursor |
| `<state-dir>/compass/events/<sid>.json`, `<sid>.heuristics.jsonl` | Classified events; heuristic rows |
| `<state-dir>/compass/rules.md` | The table Claude reads — generated, never hand-edited |
| `<state-dir>/compass/rules_manual.json` | Manual and accepted rows |
| `<state-dir>/compass/observations/` | Read-only history of the retired pipeline |
