---
type: architecture
title: "Compass measurement programme"
scope: project
description: How compass is measured — offline predictive validity, the live A/B, the doctor health check, and a proposed keep/delete rule
framework_version: "1.0"
last_verified: 2026-08-28
---

# Compass measurement programme

The 2026-08 deep review kept compass on one condition: **measure it**
(`git show 5b95eaa:docs/review/review-for-llm.md` §5a-H; decision record §6 row 3). The
profile has been injected into every session start for months and there was
no evidence either way that it changes how Claude responds. This document
describes the three instruments that now exist, the numbers to look at, and
a **proposed** rule for deciding whether to keep or delete compass.

The decision rule in §6 below is a **proposal for the human**. Nothing acts
on it automatically.

---

## 1. What compass costs today

The ledger the decision is against, measured 2026-08-26:

| Cost | Measured value |
|---|---|
| Every session's prompt | `personality.md` is 4,183 chars ≈ **1,050 input tokens**, injected by `core/hooks/startup_prompt_hook.py` at every session start |
| Weekly synthesis | ~38,300 input + ~1,200 output tokens per run ≈ **$0.22/week** at Opus list price (a subscription-plan CLI is billed differently) |
| Capture | 3–7 observations written by `/wrapup` Step 4 — inline, no extra model call |
| Storage | 71 observation files, 408 observations, 2026-04-17 → 2026-08-26 |

So the question is not "is compass expensive" — it is cheap. The question is
whether ~1,050 tokens of every prompt buy anything at all.

---

## 2. Three instruments

| Instrument | Command | Measures | Can it prove compass works? |
|---|---|---|---|
| Offline predictive validity | `compass/evaluate.py offline` | Does a profile built from other sessions predict a held-out session's traits? | **No** — only that the profile is internally consistent |
| Live A/B | `compass/evaluate.py ab` | Do sessions *with* the profile go differently from sessions *without* it? | **Yes** — this is the real test |
| Health | `apiary doctor compass` | Is the pipeline alive (observations flowing, synthesis fresh, arms balanced)? | No — it is a smoke alarm |

Read the distinction before quoting a number. The offline metric is cheap
and repeatable and it is a *necessary* condition: if a profile synthesised
from 70 sessions cannot predict the 71st, it certainly cannot inform a
response. But both sides of that comparison are text a model wrote about the
user, so a high score means the pipeline agrees with itself — not that
Claude behaves differently when it reads the profile. Only the A/B can say
that.

---

## 3. Instrument 1 — offline predictive validity

```bash
python compass/evaluate.py offline                    # stub synthesiser, free, instant
python compass/evaluate.py offline --model opus --max-folds 20   # prints a cost estimate
python compass/evaluate.py offline --model opus --max-folds 20 --yes   # actually spends it
python compass/evaluate.py labels                     # the target definition
```

### The metric, precisely

* **Unit** — one `(session, dimension)` pair.
* **Fold** — leave-one-out over the active observation files. For each
  held-out session, a profile is synthesised from *the other* sessions.
* **Target** — one label per dimension, from a fixed vocabulary. The labels
  are the **poles named in that dimension's own `description`** in
  `compass/dimensions.json` (`communication_style` → terse | verbose,
  `autonomy` → broad | gated, `mood_tone` → positive | negative | neutral,
  and so on). They are not invented for the metric.
  `compass/label_vocabulary.json` holds the cue lists.
* **Reduction** — lowercase the observation's `observation` text (never its
  `evidence`, which is a raw session quote and carries the user's words
  rather than the claim), count case-insensitive substring hits per label,
  take the argmax. Zero hits or a tie abstains; the pair is dropped from
  accuracy and counted under **coverage**.
* **Prediction** — the same reduction applied to the fold's profile section
  for that dimension. A profile with no section for a dimension abstains.
* **Headline** — micro accuracy over every scored pair. Macro accuracy (mean
  of per-dimension accuracies) is printed beside it.
* **Baselines** — *majority*: per fold, the most common label for that
  dimension among the training sessions, i.e. what you would predict with no
  model at all. *Random*: the analytic expectation `mean(1/|labels|)`, no RNG.
* **Lift** — headline minus majority, in percentage points.

**Lift over majority is the number that matters.** A headline of 86% sounds
good and means nothing if the majority baseline is also 86% — that says the
profile is restating the user's single most common trait, which one line of
code could do without an Opus call.

### Synthesisers

`--dry-run` (and the no-flag default) uses a deterministic **stub**: it
concatenates the training observations per dimension, respecting the same
volatile-window rule as the real prompt. It exists so the metric pipeline is
testable and so the test suite never spawns a model. Its numbers are a
**floor**, not evidence.

`--model MODEL` runs the real `compass/synthesize.py` prompt once per fold.
That is one `claude -p` call per fold; the cost estimate is printed first and
`--yes` is required before anything is spent. At Opus list price this
corpus costs ≈ **$0.30/fold** — ≈ $6 for a 20-fold sample, ≈ $21 for all 70.
Neither `personality.md` nor `corrections.md` is fed into a fold: both are
derived from the full history and would leak the held-out session's answer.

### What the stub run says today (2026-08-26, 71 sessions)

```
folds:    70 (leave-one-out)
coverage: 288/408 (session, dimension) pairs reduced to a label (70.6%)
HEADLINE   micro accuracy      76.7%
           majority baseline   77.4%
           random baseline     49.1%
           LIFT over majority  -0.7 pts
```

Zero lift, exactly as expected — the stub *is* a keyword summary of the
training text, so it cannot beat a keyword majority. That is the point of
running it: the pipeline works, the baselines are wired correctly, and the
interesting question is now sharply posed. **Does a real Opus synthesis beat
77.4%?** That has not been run yet (see §7).

Per-dimension headroom differs a lot, and this is itself a finding:

| Dimension | n | majority | Discriminative? |
|---|---|---|---|
| `communication_style` | 56 | 100% | No — the label never varies |
| `meta_awareness` | 18 | 100% | No — the label never varies |
| `risk_tolerance` | 17 | 88% | Barely |
| `trust_calibration` | 32 | 88% | Barely |
| `engagement` | 34 | 74% | Yes |
| `pushback` | 31 | 71% | Yes |
| `mood_tone` | 15 | 67% | Yes |
| `autonomy` | 42 | 62% | Yes |
| `decision_making` | 42 | 52% | Yes — near coin-flip |

Dimensions whose label never varies contribute a free 100% to the headline
and zero information. When reading a model run, look at `autonomy`,
`decision_making`, `engagement`, `pushback` and `mood_tone` — those are where
a profile can actually earn its keep.

---

## 4. Instrument 2 — the live A/B

### How it works

Every session lands in arm `on` (profile injected — today's behaviour) or
`off` (no injection). The arm is a deterministic function of the session id
and a seed, not an RNG draw: `sha256("<ab_seed>:<sid8>")`'s first 32 bits
divided by 2³², compared against `ab_on_fraction`. That means any consumer
can ask for a session's arm at any time and get the same answer, and an
analysis re-run months later reproduces the same split.

* `core/startup.py:run_init` stamps `compass_arm` into
  `<state-dir>/sessions/identity-<sid8>.json`, so a later `ab_seed` change
  cannot rewrite what already happened. A recorded arm always wins over a
  recomputation.
* `core/hooks/startup_prompt_hook.py` skips the profile block when the arm
  is `off`. Any failure in that lookup means "inject" — a broken config can
  never silently strip the profile.

### It is OFF by default

`compass/config.json` ships `ab_enabled: false`. While it is false,
`arm_for_session` returns `on` for **every** session and nothing changes for
the user. To start the experiment:

```jsonc
// compass/config.json
{
  "ab_enabled": true,          // <- the only edit needed
  "ab_seed": "compass-ab-2026-08",
  "ab_on_fraction": 0.5
}
```

Change `ab_seed` only to start a fresh measurement window; changing it
mid-window re-rolls every future session and makes the two halves
incomparable. `$APIARY_COMPASS_CONFIG` points at an alternate file (tests
use it; so could a second machine).

### Reading the result

```bash
python compass/evaluate.py ab --since 2026-09-01
python compass/evaluate.py ab --json
```

Three proxies, joined from the budgeter log. **How far to trust each is part
of the output, not a footnote:**

| Proxy | Definition | Honest? |
|---|---|---|
| `tool_calls_per_task` | budgeter log rows per `(session_id, task_turn)` | **Honest but confounded** — hard tasks need more tool calls and the arms are not matched on difficulty. Only meaningful across many sessions. |
| `corrections_per_task` | distinct user turns matching a correction keyword list, per task | **The most direct outcome proxy and the noisiest.** A keyword heuristic over free text: it fires on "no need to" and misses a polite redirect. Directional only. |
| `net_tokens_per_task` | `net_tokens_delta` summed per task | **Not an outcome measure.** The injected profile is itself ~1,050 prompt tokens, so the `on` arm pays for it by construction. Report it as the cost side of the ledger, never as "compass made sessions more expensive". |

The command prints a warning while either arm has fewer than **30 sessions**
— below that the numbers are noise.

---

## 5. Instrument 3 — health

```bash
apiary doctor compass      # or `apiary doctor` to run it with everything else
```

Report-only by design: every finding is a *note*, never an *issue*, so a
stale profile can never fail a doctor run that CI gates on. It reports
active/archived observation counts, `personality.md` size and synthesis age
(warning above 14 days), the A/B arm counts recorded in session identity
files, and the last `evaluate.py offline` headline cached at
`<state-dir>/compass/evaluate/last.json`.

Arm counts are a **rolling ~30-day window**, not a lifetime total:
`core/session.py` sweeps identity files older than `IDENTITY_MAX_AGE_DAYS`.
For a full-window count, use `evaluate.py ab`, which joins against the
budgeter log instead.

---

## 6. Proposed decision rule — **for the human to accept or change**

Review §6 asked for a rule that decides compass "on the numbers". This is the
proposal. It is not implemented anywhere and nothing enforces it.

**Gate A — offline, ~$6, runnable today**

Run `compass/evaluate.py offline --model opus --max-folds 20 --yes` once,
after the Phase 1.4/1.8 fixes have produced a fresh synthesis.

* **Lift ≥ +5 points** over the majority baseline → the profile carries
  information a trivial rule does not. Proceed to Gate B.
* **Lift between 0 and +5** → weak. Proceed to Gate B anyway; the offline
  metric is coarse enough that this band means "undecided".
* **Lift ≤ 0** → one strike. The profile's dimension-level content is
  redundant with a one-line "this user is terse, directive, and verifies"
  rule. Not a delete on its own — the profile may carry nuance the label
  reducer cannot see — but it raises the bar Gate B has to clear.

**Gate B — the live A/B, the decision that counts**

Set `ab_enabled: true`, then wait for **≥ 30 sessions in each arm**. At the
observed capture rate (71 observations over 131 days ≈ 0.54/day) a 50/50
split reaches 30 per arm in roughly **111 days**. Proposed review date:
**2026-12-15**.

* **KEEP** if the `on` arm shows **≥ 10% fewer `corrections_per_task`** than
  `off`, or **≥ 10% fewer `tool_calls_per_task`**, with both arms ≥ 30
  sessions.
* **DELETE** if *neither* proxy differs by more than **5%** in either
  direction *and* Gate A showed no lift. Compass would then be costing
  ~1,050 tokens of every prompt and a weekly Opus call for an effect too
  small to detect with 60 sessions — which is the same as no effect.
  Deleting means: stop the injection, stop the cron, keep the observation
  files (they are cheap and are the only behavioural record that exists).
* **SHRINK, don't delete**, if the result is inconclusive (a difference
  between 5% and 10%, or the arms disagree between the two proxies): cut the
  injected profile to the three dimensions with the highest per-dimension
  offline accuracy, which roughly halves the token cost, and re-run the A/B
  for one more window.

Whatever the outcome, record it as a scribe decision note and update this
document's `last_verified`.

---

## 7. Limitations — read these before quoting a number

1. **The offline metric measures self-consistency, not effect.** Both sides
   are model-written text about the user. It cannot, in principle, show that
   injecting the profile changes a response.
2. **The label reducer is coarse.** It is substring counting with no negation
   handling: "not at all terse" scores as *terse*. Coverage is 70.6% on the
   current corpus — the other 29.4% of observations resolve to no label at
   all and are silently outside the metric. The coverage figure is printed
   next to the headline for exactly this reason.
3. **Two dimensions carry no variance.** `communication_style` and
   `meta_awareness` are 100% one label across all 71 sessions, so they inflate
   the headline without informing it.
4. **The vocabulary is v1 and must be frozen during a measurement window.**
   Editing `label_vocabulary.json` changes the metric. Change it before a
   window, never inside one, and never to make a number move.
5. **The observations themselves are model-written.** If `/wrapup` capture is
   biased toward confirming the existing profile, the offline metric will
   reward that bias. The A/B is immune to this; the offline metric is not.
6. **`corrections_per_task` is a keyword heuristic.** Treat a 10% difference
   as a signal to look closer, not as a measurement.
7. **The current profile is stale.** `personality.md` says "synthesized from
   7 sessions, last updated 2026-04-17" while 71 observation files exist — the
   cron self-throttle bug (review 1.4). Any A/B started before a fresh
   synthesis is measuring a four-month-old profile, which is a different
   experiment from the one worth running. **Run `/compass-sync` first.**

---

## 8. Files

| Path | What it is |
|---|---|
| `compass/evaluate.py` | `offline`, `ab`, `labels` — the measurement CLI |
| `compass/label_vocabulary.json` | The target definition (labels + cue lists) |
| `compass/ab.py` | Arm assignment, config loading |
| `compass/config.json` | `ab_enabled` / `ab_seed` / `ab_on_fraction` |
| `compass/health.py` | The facts behind `apiary doctor compass` |
| `<state-dir>/compass/evaluate/last.json` | Cached headline, read by the doctor |
