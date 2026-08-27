#!/usr/bin/env python3
"""Compass measurement — is the personality profile worth its context budget?

The 2026-08 deep review kept compass on condition that it be *measured*
(review §5a-H, decision §6 row 3): the profile has been injected at every
session start for months and there is no evidence either way that it
changes how Claude responds. This module is the instrument.

Two commands, measuring two different things. Read the difference before
quoting either number:

``offline`` — **predictive validity (internal)**
    Leave-one-out over the per-session observation files. For each held-out
    session, synthesise a profile from *the other* sessions, reduce both the
    profile and the held-out session's observations to one label per
    dimension, and ask: did the profile predict this session's labels?
    Scored against a majority-label baseline and a random baseline.

    What it can prove: the profile is **stable** — what compass says about
    the user generalises from one set of sessions to a session it has never
    seen. That is a *necessary* condition for the profile to be worth
    injecting.

    What it cannot prove: that injecting it changes Claude's behaviour.
    Both sides of the comparison are text a model wrote about the user, so a
    high score means the pipeline agrees with itself. Do not report this as
    "compass works".

``ab`` — **live effect (external)**
    Joins the per-session A/B arm (``compass/ab.py``) against outcome
    proxies from the budgeter log. This is the one that can say the profile
    made a difference — and it needs the experiment turned on and ~30
    sessions per arm before it says anything at all.

Metric definition (``offline``)
-------------------------------
* **Unit**: one ``(session, dimension)`` pair.
* **Target**: one label per dimension from a fixed vocabulary whose labels
  are the poles named in that dimension's ``description`` in
  ``dimensions.json`` (e.g. ``communication_style`` → terse | verbose).
  ``compass/label_vocabulary.json`` holds the cues; the reduction is a
  case-insensitive substring-count argmax over the ``observation`` text
  (never ``evidence`` — that is a raw session quote). Zero hits or a tie
  yields no label: the pair is dropped from accuracy and counted as
  coverage lost. This is a *coarse* reduction and it is the main threat to
  the metric's validity; the coverage number is printed so you can judge it.
* **Prediction**: the same reduction applied to the held-out fold's
  synthesised profile section for that dimension. A profile with no section
  for the dimension abstains (dropped, counted).
* **Headline**: micro accuracy over every evaluated pair. Macro accuracy
  (mean of per-dimension accuracies) is printed beside it.
* **Baselines**: *majority* — per fold, the most common label for that
  dimension among the training sessions, which is what you would predict
  with no model at all; *random* — the analytic expectation
  ``mean(1 / |labels(dimension)|)``, no RNG.
* **Lift**: headline − majority, in percentage points. **Lift over majority
  is the number that matters.** A high headline with zero lift means the
  profile is only restating the user's most common label, which one line of
  code could do without an Opus call.

Synthesisers
------------
``--dry-run`` (and the no-flag default) uses a deterministic stub that
concatenates the training observations per dimension. It exists so the
metric pipeline is testable and so the tests never spawn a model; its
numbers are a *floor*, not evidence. ``--model MODEL`` runs the real
``compass/synthesize.py`` prompt once per fold — that is N model calls, so
the cost estimate is printed and confirmation is required.

Exit codes:
  0 — evaluation ran
  1 — not enough data (fewer than two labelled sessions / no log rows)
  2 — usage error, or the model run was declined
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import ab, store  # noqa: E402
from compass import synthesize as synth  # noqa: E402
from core.utils.atomic import write_text_atomic  # noqa: E402

VOCABULARY_FILE = Path(__file__).resolve().parent / "label_vocabulary.json"

EVALUATE_DIRNAME = "evaluate"
LAST_RESULT_FILENAME = "last.json"

# Volatile dimensions are current-state, not personality: the real prompt
# only lets the newest few sessions speak for them, so the stub does too.
STUB_VOLATILE_WINDOW = synth.VOLATILE_RECENT_WINDOW

# Rough token accounting for the cost estimate. 4 chars/token is the usual
# English approximation; the synthesis prompt is JSON-heavy so it errs low
# rather than high, which is the wrong direction for a spend warning —
# hence the explicit "estimate, not a quote" wording when it prints.
CHARS_PER_TOKEN = 4
EST_OUTPUT_TOKENS = 1200          # the prompt asks for 400-800 words

# Per-MTok list prices (USD, Anthropic first-party API, 2026-08). Only used
# to turn a token estimate into a number a human can react to. Sessions
# driven by the `claude` CLI on a subscription plan are not billed at these
# rates — the estimate is an order-of-magnitude guide, not an invoice.
MODEL_PRICES = {
    "opus": (5.0, 25.0),
    "sonnet": (2.0, 10.0),
    "haiku": (1.0, 5.0),
}

# --------------------------------------------------------------------------
# A/B outcome proxies
# --------------------------------------------------------------------------
# Substrings that mark a user turn as a correction / redirect. Deliberately
# short and deliberately crude: this is a keyword heuristic over free text
# and it will fire on "no need to" and miss a polite redirect. It is here
# because it is the only signal in the log that points at *quality* rather
# than volume — treat the number as directional, never as a measurement.
CORRECTION_CUES = (
    "no, ", "no i ", "nope", "not what", "not right", "that's wrong",
    "thats wrong", "incorrect", "actually,", "actually i", "revert",
    "undo", "roll back", "rollback", "stop,", "stop.", "wait,", "wait ",
    "why did you", "you were supposed", "i said", "i asked for",
    "that's not", "thats not", "don't ", "dont ", "instead of",
)


# --------------------------------------------------------------------------
# Label reduction — the target definition
# --------------------------------------------------------------------------

def load_vocabulary(path: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """Return ``{dimension: {label: [cue, ...]}}`` from the vocabulary file."""
    data = json.loads((path or VOCABULARY_FILE).read_text(encoding="utf-8"))
    labels = data.get("labels", {})
    if not isinstance(labels, dict):
        raise ValueError(f"{path or VOCABULARY_FILE}: 'labels' is not an object")
    return labels


def reduce_label(text: str, dimension: str,
                 vocabulary: dict[str, dict[str, list[str]]]) -> str | None:
    """Reduce free text to one label for *dimension*, or None.

    Counts case-insensitive substring occurrences of every cue and returns
    the argmax. Returns None when no cue hits at all, or when the top two
    labels tie — an unresolved pair is dropped from the metric rather than
    guessed, so coverage stays visible instead of hiding in the accuracy.
    """
    labels = vocabulary.get(dimension)
    if not labels or not text:
        return None
    hay = text.lower()
    scores: list[tuple[int, str]] = []
    for label, cues in sorted(labels.items()):
        score = sum(hay.count(cue.lower()) for cue in cues)
        scores.append((score, label))
    scores.sort(key=lambda item: (-item[0], item[1]))
    if not scores or scores[0][0] == 0:
        return None
    if len(scores) > 1 and scores[0][0] == scores[1][0]:
        return None
    return scores[0][1]


# --------------------------------------------------------------------------
# Observation loading
# --------------------------------------------------------------------------

def load_sessions(paths: list[Path] | None = None) -> list[dict]:
    """Load active observation files, newest ``captured_at`` first.

    Same loader and same ordering as ``synthesize.py`` so a fold sees the
    observations in the order the real synthesis prompt would show them.
    """
    paths = store.list_active_observations() if paths is None else paths
    return synth._load_active(paths)


def session_labels(session: dict, vocabulary: dict) -> dict[str, str]:
    """The labels this session's own observations carry, per dimension.

    When a session says two things about one dimension, their ``observation``
    texts are concatenated and reduced together — the session gets one label
    per dimension, matching the shape of a profile section.
    """
    texts: dict[str, list[str]] = defaultdict(list)
    for obs in session.get("observations", []):
        if not isinstance(obs, dict):
            continue
        dim = obs.get("dimension")
        text = obs.get("observation")
        if isinstance(dim, str) and isinstance(text, str):
            texts[dim].append(text)
    out: dict[str, str] = {}
    for dim, parts in texts.items():
        label = reduce_label(" ".join(parts), dim, vocabulary)
        if label is not None:
            out[dim] = label
    return out


# --------------------------------------------------------------------------
# Synthesisers
# --------------------------------------------------------------------------

def stub_synthesize(training: list[dict], dimensions: dict) -> str:
    """Deterministic, model-free stand-in for ``compass/synthesize.py``.

    Emits one ``## <dimension>`` section per dimension that has any training
    observation, filled with those observations' text. It is the *floor* the
    real synthesiser has to beat: everything a keyword reducer could learn
    from the raw material, with no model in the loop. Volatile dimensions
    see only the newest sessions, mirroring the real prompt's rule.
    """
    volatile = {d["name"] for d in dimensions["dimensions"] if d.get("volatile")}
    order = [d["name"] for d in dimensions["dimensions"]]
    per_dim: dict[str, list[str]] = defaultdict(list)
    for rank, session in enumerate(training):  # training is newest-first
        for obs in session.get("observations", []):
            if not isinstance(obs, dict):
                continue
            dim = obs.get("dimension")
            text = obs.get("observation")
            if not isinstance(dim, str) or not isinstance(text, str):
                continue
            if dim in volatile and rank >= STUB_VOLATILE_WINDOW:
                continue
            per_dim[dim].append(text)

    lines = ["# Personality profile (stub synthesis)", "",
             f"_Stub-synthesized from {len(training)} session(s)._", ""]
    for dim in order:
        if not per_dim.get(dim):
            continue
        lines.append(f"## {dim}")
        lines.append(" ".join(per_dim[dim]))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def model_synthesize(training: list[dict], dimensions: dict, model: str) -> str:
    """Run the real synthesis prompt for one fold. Returns "" on failure.

    ``previous_personality`` and ``corrections`` are deliberately passed
    empty. Both are derived from the full history — the live
    ``personality.md`` was synthesised from observations that include the
    held-out session — so feeding them in would leak the answer into the
    fold and inflate the score.
    """
    prompt = synth._build_prompt(training, "", "", dimensions)
    rc, stdout, stderr = synth.run_claude(prompt, model=model)
    if rc != 0:
        print(f"  fold synthesis failed (rc={rc}): {stderr.strip()[:200]}", file=sys.stderr)
        return ""
    return synth._extract_markdown(stdout)


PROFILE_SECTION_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def profile_sections(profile: str) -> dict[str, str]:
    """Split a synthesised profile into ``{dimension_name: section_text}``.

    Headings are normalised (lowercased, spaces and hyphens to underscores)
    so ``## Communication style`` and ``## communication_style`` both land on
    the same dimension — the model is asked for the latter but does not
    always comply.
    """
    out: dict[str, str] = {}
    matches = list(PROFILE_SECTION_RE.finditer(profile or ""))
    for i, match in enumerate(matches):
        name = match.group(1).strip().lower()
        name = re.sub(r"[\s\-]+", "_", name)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(profile)
        out[name] = profile[match.end():end].strip()
    return out


# --------------------------------------------------------------------------
# The fold loop
# --------------------------------------------------------------------------

def evaluate_folds(sessions: list[dict], vocabulary: dict, dimensions: dict,
                   *, synthesizer, max_folds: int | None = None,
                   progress=None) -> dict:
    """Leave-one-out evaluation. Returns the full result record.

    *synthesizer* is called as ``synthesizer(training_sessions)`` and must
    return profile markdown; that is the only difference between the stub
    and the model run.
    """
    truths = [session_labels(s, vocabulary) for s in sessions]
    eligible = [i for i, labels in enumerate(truths) if labels]

    # How much of the raw material the reducer could actually label. A low
    # coverage means the vocabulary, not compass, is what the headline is
    # measuring — so it is reported next to the headline, never hidden.
    present_pairs = {
        (i, obs.get("dimension"))
        for i, session in enumerate(sessions)
        for obs in session.get("observations", [])
        if isinstance(obs, dict) and isinstance(obs.get("dimension"), str)
    }
    labelled_pairs = sum(len(t) for t in truths)

    result: dict = {
        "sessions_total": len(sessions),
        "sessions_labelled": len(eligible),
        "folds": 0,
        "pairs_evaluated": 0,
        "pairs_labelled": labelled_pairs,
        "pairs_present": len(present_pairs),
        "label_coverage": labelled_pairs / len(present_pairs) if present_pairs else None,
        "abstentions": 0,
        "correct": 0,
        "headline": None,
        "macro": None,
        "majority": None,
        "random": None,
        "lift_over_majority": None,
        "per_dimension": {},
    }
    if len(eligible) < 2:
        return result

    if max_folds is not None and max_folds > 0:
        eligible = eligible[:max_folds]

    # per dimension: [(truth, prediction)], plus the majority baseline's calls
    pairs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    majority_hits: dict[str, int] = defaultdict(int)
    majority_seen: dict[str, int] = defaultdict(int)

    for fold_no, held_out in enumerate(eligible, start=1):
        training = [s for i, s in enumerate(sessions) if i != held_out]
        if progress:
            progress(fold_no, len(eligible))
        profile = synthesizer(training)
        sections = profile_sections(profile)

        for dim, truth in sorted(truths[held_out].items()):
            section = sections.get(dim)
            predicted = reduce_label(section, dim, vocabulary) if section else None
            if predicted is None:
                result["abstentions"] += 1
                continue
            pairs[dim].append((truth, predicted))

            # majority baseline over the same training split
            train_labels = [truths[i].get(dim) for i in range(len(sessions))
                            if i != held_out and truths[i].get(dim)]
            if train_labels:
                majority = sorted(Counter(train_labels).items(),
                                  key=lambda kv: (-kv[1], kv[0]))[0][0]
                majority_seen[dim] += 1
                if majority == truth:
                    majority_hits[dim] += 1

        result["folds"] += 1

    total_pairs = sum(len(v) for v in pairs.values())
    total_correct = sum(1 for v in pairs.values() for truth, pred in v if truth == pred)
    result["pairs_evaluated"] = total_pairs
    result["correct"] = total_correct

    accuracies: list[float] = []
    for dim in sorted(pairs):
        dim_pairs = pairs[dim]
        correct = sum(1 for truth, pred in dim_pairs if truth == pred)
        accuracy = correct / len(dim_pairs)
        accuracies.append(accuracy)
        precision: dict[str, dict] = {}
        for label in sorted({pred for _, pred in dim_pairs}):
            predicted_n = sum(1 for _, pred in dim_pairs if pred == label)
            true_positive = sum(1 for truth, pred in dim_pairs
                                if pred == label and truth == label)
            precision[label] = {
                "predicted": predicted_n,
                "correct": true_positive,
                "precision": true_positive / predicted_n,
            }
        maj_seen = majority_seen.get(dim, 0)
        result["per_dimension"][dim] = {
            "n": len(dim_pairs),
            "correct": correct,
            "accuracy": accuracy,
            "majority_accuracy": (majority_hits.get(dim, 0) / maj_seen) if maj_seen else None,
            "labels": sorted(vocabulary.get(dim, {})),
            "precision": precision,
        }

    if total_pairs:
        result["headline"] = total_correct / total_pairs
        result["macro"] = sum(accuracies) / len(accuracies)
        seen = sum(majority_seen.values())
        if seen:
            result["majority"] = sum(majority_hits.values()) / seen
            result["lift_over_majority"] = result["headline"] - result["majority"]
        result["random"] = sum(
            1.0 / max(1, len(vocabulary.get(dim, {}))) * len(pairs[dim]) for dim in pairs
        ) / total_pairs
    return result


# --------------------------------------------------------------------------
# Result cache (read by `apiary doctor compass`)
# --------------------------------------------------------------------------

def evaluate_dir() -> Path:
    return store.compass_dir() / EVALUATE_DIRNAME


def last_result_path() -> Path:
    return evaluate_dir() / LAST_RESULT_FILENAME


def cache_result(result: dict) -> Path:
    """Persist the headline so the doctor can report it without recomputing."""
    payload = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "mode": result.get("mode"),
        "headline": result.get("headline"),
        "macro": result.get("macro"),
        "majority": result.get("majority"),
        "random": result.get("random"),
        "lift_over_majority": result.get("lift_over_majority"),
        "folds": result.get("folds"),
        "pairs_evaluated": result.get("pairs_evaluated"),
        "sessions_labelled": result.get("sessions_labelled"),
    }
    path = last_result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(payload, indent=2) + "\n")
    return path


def load_cached_result() -> dict | None:
    try:
        data = json.loads(last_result_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# Cost estimate for the model path
# --------------------------------------------------------------------------

def estimate_cost(sessions: list[dict], dimensions: dict, folds: int,
                  model: str) -> dict:
    """Estimated tokens and USD for a model-backed run of *folds* folds."""
    if not sessions:
        return {"folds": 0, "input_tokens": 0, "output_tokens": 0, "usd": None}
    sample = synth._build_prompt(sessions[1:], "", "", dimensions)
    per_fold_input = max(1, len(sample) // CHARS_PER_TOKEN)
    total_input = per_fold_input * folds
    total_output = EST_OUTPUT_TOKENS * folds
    prices = MODEL_PRICES.get(str(model).split("-")[0].lower())
    usd = None
    if prices:
        usd = (total_input / 1_000_000) * prices[0] + (total_output / 1_000_000) * prices[1]
    return {
        "folds": folds,
        "per_fold_input_tokens": per_fold_input,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "usd": usd,
    }


# --------------------------------------------------------------------------
# `offline` command
# --------------------------------------------------------------------------

def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _print_offline(result: dict) -> None:
    print(f"compass offline predictive validity — {result['mode']} synthesiser")
    print(f"  sessions: {result['sessions_total']} active, "
          f"{result['sessions_labelled']} with at least one labelled dimension")
    print(f"  folds:    {result['folds']} (leave-one-out)")
    print(f"  pairs:    {result['pairs_evaluated']} scored, "
          f"{result['abstentions']} abstained (profile had no resolvable label)")
    print(f"  coverage: {result['pairs_labelled']}/{result['pairs_present']} "
          f"(session, dimension) pairs reduced to a label "
          f"({_fmt_pct(result['label_coverage'])})")
    print()
    if not result["pairs_evaluated"]:
        print("  no scored pairs — nothing to report")
        return

    lift = result["lift_over_majority"]
    lift_text = "n/a" if lift is None else f"{lift * 100:+.1f} pts"
    print(f"  HEADLINE   micro accuracy      {_fmt_pct(result['headline'])}")
    print(f"             macro accuracy      {_fmt_pct(result['macro'])}")
    print(f"             majority baseline   {_fmt_pct(result['majority'])}")
    print(f"             random baseline     {_fmt_pct(result['random'])}")
    print(f"             LIFT over majority  {lift_text}")
    print()
    print(f"  {'dimension':<22} {'n':>4} {'acc':>7} {'majority':>9}  per-label precision")
    for dim, stats in sorted(result["per_dimension"].items()):
        precision = ", ".join(
            f"{label} {info['correct']}/{info['predicted']}"
            f" ({info['precision'] * 100:.0f}%)"
            for label, info in sorted(stats["precision"].items())
        )
        print(f"  {dim:<22} {stats['n']:>4} {_fmt_pct(stats['accuracy']):>7} "
              f"{_fmt_pct(stats['majority_accuracy']):>9}  {precision}")
    print()
    print("  Reading this: lift over majority is the number that matters. "
          "A headline that\n  matches the majority baseline means the profile "
          "adds nothing a one-line rule\n  could not. This measures the "
          "profile's INTERNAL consistency, not whether\n  injecting it changes "
          "Claude's behaviour — that is `evaluate.py ab`.")
    if result["mode"] == "stub":
        print("  The stub synthesiser is a no-model floor: these numbers test the "
              "pipeline,\n  they are not evidence about compass.")


def cmd_offline(args: argparse.Namespace) -> int:
    vocabulary = load_vocabulary()
    dimensions = store.load_dimensions()
    sessions = load_sessions()
    if len(sessions) < 2:
        print(f"need at least 2 valid observation files, found {len(sessions)}",
              file=sys.stderr)
        return 1

    use_model = bool(args.model) and not args.dry_run
    if use_model:
        labelled = sum(1 for s in sessions if session_labels(s, vocabulary))
        folds = min(labelled, args.max_folds) if args.max_folds else labelled
        estimate = estimate_cost(sessions, dimensions, folds, args.model)
        # The estimate goes to stderr so `--json` stdout stays parseable.
        say = lambda line: print(line, file=sys.stderr)  # noqa: E731
        say(f"model run: {folds} fold(s) × 1 `claude -p` call each, model={args.model!r}")
        say(f"  estimated input  ~{estimate['input_tokens']:,} tokens "
            f"(~{estimate['per_fold_input_tokens']:,}/fold)")
        say(f"  estimated output ~{estimate['output_tokens']:,} tokens")
        if estimate["usd"] is not None:
            say(f"  ≈ ${estimate['usd']:.2f} at {args.model} API list price "
                f"— an estimate, not a quote; a subscription-plan CLI is billed "
                f"differently")
        else:
            say(f"  (no list price known for model {args.model!r})")
        if not args.yes:
            say("\nre-run with --yes to spend this. Nothing was called.")
            return 2

        def progress(i, n):
            print(f"  fold {i}/{n} …", file=sys.stderr)

        def synthesizer(training):
            return model_synthesize(training, dimensions, args.model)
    else:
        progress = None

        def synthesizer(training):
            return stub_synthesize(training, dimensions)

    result = evaluate_folds(
        sessions, vocabulary, dimensions,
        synthesizer=synthesizer,
        max_folds=args.max_folds,
        progress=progress,
    )
    result["mode"] = "model" if use_model else "stub"
    result["model"] = args.model if use_model else None

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_offline(result)

    if result["pairs_evaluated"] and not args.no_cache:
        try:
            path = cache_result(result)
            if not args.json:
                print(f"\n  cached headline → {path}")
        except OSError as exc:
            print(f"could not cache the result: {exc}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# `ab` command
# --------------------------------------------------------------------------

def read_budgeter_log(path: Path | None = None) -> list[dict]:
    """Read the budgeter usage log through budgeter's own reader.

    Swaps the module global rather than re-implementing the JSONL parse —
    there are already two copies of that loop in the tree (review X-3) and
    this is not going to be the third.
    """
    from budgeter.lib import logger as budget_logger
    if path is None:
        return budget_logger.read_log()
    original = budget_logger.LOG_PATH
    budget_logger.LOG_PATH = Path(path)
    try:
        return budget_logger.read_log()
    finally:
        budget_logger.LOG_PATH = original


def is_correction(message: str) -> bool:
    """Keyword heuristic: does this user turn read as a correction?"""
    if not message:
        return False
    text = " " + message.strip().lower() + " "
    return any(cue in text for cue in CORRECTION_CUES)


def summarise_arms(rows: list[dict], *, since: str | None = None,
                   arm_lookup=None) -> dict:
    """Group budgeter log rows by A/B arm and compute the outcome proxies.

    Proxies, and how far to trust each:

    ``tool_calls_per_task``
        Log rows per ``(session_id, task_turn)``. One row is one tool call
        attributed to a turn. **Honest but confounded**: a hard task needs
        more tool calls than an easy one, and the arms are not matched on
        task difficulty. Only meaningful across many sessions.

    ``corrections_per_task``
        Distinct user turns matching :data:`CORRECTION_CUES`, per task.
        **The most direct outcome proxy and the noisiest** — a keyword
        heuristic over free text. Directional only.

    ``net_tokens_per_task``
        ``net_tokens_delta`` summed per task. **Not an honest outcome
        measure**: the injected profile is itself several KB of prompt, so
        the ``on`` arm pays for it in this number by construction. Report it
        as the *cost* side of the ledger, never as "compass made sessions
        more expensive".
    """
    arm_lookup = arm_lookup or ab.arm_for_session

    by_session: dict[str, dict] = defaultdict(
        lambda: {"rows": 0, "tasks": set(), "tokens": 0, "turns": {}}
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = str(row.get("timestamp", ""))
        if since and timestamp[:10] < since:
            continue
        sid = str(row.get("session_id", ""))
        if not sid:
            continue
        bucket = by_session[sid]
        bucket["rows"] += 1
        bucket["tasks"].add(row.get("task_turn", row.get("turn_number", 0)))
        bucket["tokens"] += int(row.get("net_tokens_delta", 0) or 0)
        turn = row.get("turn_number", 0)
        message = row.get("user_message", "")
        if message and turn not in bucket["turns"]:
            bucket["turns"][turn] = message

    arms: dict[str, dict] = {
        arm: {"sessions": 0, "rows": 0, "tasks": 0, "tokens": 0,
              "user_turns": 0, "corrections": 0}
        for arm in ab.ARMS
    }
    for sid, bucket in by_session.items():
        arm = arm_lookup(sid)
        if arm not in arms:
            continue
        slot = arms[arm]
        slot["sessions"] += 1
        slot["rows"] += bucket["rows"]
        slot["tasks"] += len(bucket["tasks"])
        slot["tokens"] += bucket["tokens"]
        slot["user_turns"] += len(bucket["turns"])
        slot["corrections"] += sum(1 for m in bucket["turns"].values() if is_correction(m))

    for slot in arms.values():
        tasks = slot["tasks"]
        slot["tool_calls_per_task"] = slot["rows"] / tasks if tasks else None
        slot["net_tokens_per_task"] = slot["tokens"] / tasks if tasks else None
        slot["corrections_per_task"] = slot["corrections"] / tasks if tasks else None

    return {"arms": arms, "sessions_seen": len(by_session), "since": since}


# Sessions per arm before the comparison is worth reading (review §5a-H.2).
AB_MIN_SESSIONS_PER_ARM = 30


def _print_ab(summary: dict, config: dict) -> None:
    enabled = bool(config.get("ab_enabled"))
    print("compass live A/B — profile injection on vs off")
    if not enabled:
        print("  STATUS: DISABLED (compass/config.json ab_enabled=false).")
        print("          Every session is in arm 'on' and the profile is injected as")
        print("          always. The 'off' row will stay empty until you enable it —")
        print("          see docs/compass-measurement.md.")
    else:
        print(f"  STATUS: running (seed={config.get('ab_seed')!r}, "
              f"on_fraction={config.get('ab_on_fraction')})")
    if summary["since"]:
        print(f"  window: rows on/after {summary['since']}")
    print(f"  sessions in log: {summary['sessions_seen']}")
    print()
    header = (f"  {'arm':<5} {'sessions':>8} {'tasks':>6} {'tool calls/task':>16} "
              f"{'corrections/task':>17} {'net tokens/task':>16}")
    print(header)
    for arm in ab.ARMS:
        slot = summary["arms"][arm]

        def fmt(value, spec=".2f"):
            return "—" if value is None else format(value, spec)

        print(f"  {arm:<5} {slot['sessions']:>8} {slot['tasks']:>6} "
              f"{fmt(slot['tool_calls_per_task']):>16} "
              f"{fmt(slot['corrections_per_task']):>17} "
              f"{fmt(slot['net_tokens_per_task'], ',.0f'):>16}")
    print()
    short = [arm for arm in ab.ARMS
             if summary["arms"][arm]["sessions"] < AB_MIN_SESSIONS_PER_ARM]
    if short:
        print(f"  n is too small to read: arm(s) {', '.join(short)} have fewer than "
              f"{AB_MIN_SESSIONS_PER_ARM} sessions.")
    print("  Honesty: tool calls/task is honest but confounded by task difficulty.")
    print("           corrections/task is the most direct proxy and the noisiest "
          "(keyword\n           heuristic over user turns) — directional only.")
    print("           net tokens/task is NOT an outcome measure: the injected profile "
          "is\n           itself prompt tokens, so arm 'on' pays for it by construction.")


def cmd_ab(args: argparse.Namespace) -> int:
    config = ab.load_config()
    rows = read_budgeter_log(Path(args.log) if args.log else None)
    if not rows:
        print("budgeter log is empty or missing; nothing to join", file=sys.stderr)
        return 1
    summary = summarise_arms(rows, since=args.since)
    if args.json:
        print(json.dumps({"config": {k: config[k] for k in ab.DEFAULT_CONFIG},
                          **summary}, indent=2, default=str))
    else:
        _print_ab(summary, config)
    return 0


# --------------------------------------------------------------------------
# `labels` command
# --------------------------------------------------------------------------

def cmd_labels(args: argparse.Namespace) -> int:
    vocabulary = load_vocabulary()
    if args.json:
        print(json.dumps(vocabulary, indent=2))
        return 0
    for dim in store.dimension_names():
        labels = vocabulary.get(dim)
        if not labels:
            print(f"{dim}: (no labels configured)")
            continue
        print(f"{dim}: {' | '.join(sorted(labels))}")
        for label, cues in sorted(labels.items()):
            print(f"    {label}: {', '.join(cues)}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure whether the compass personality profile carries signal")
    parser.add_argument("--state-dir", dest="state_dir",
                        help="evaluate another target's compass state "
                             f"(sets ${store.TARGET_STATE_DIR_ENV})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_off = sub.add_parser(
        "offline", help="leave-one-out predictive validity over observation files")
    p_off.add_argument("--dry-run", action="store_true",
                       help="force the deterministic stub synthesiser (the default "
                            "when --model is absent); never calls a model")
    p_off.add_argument("--model", default=None,
                       help="run the real synthesiser once per fold with this model "
                            "alias (costs money; prints an estimate first)")
    p_off.add_argument("--max-folds", type=int, default=None,
                       help="stop after N folds (use with --model to bound spend)")
    p_off.add_argument("--yes", action="store_true",
                       help="confirm the estimated spend and actually run --model")
    p_off.add_argument("--json", action="store_true", help="emit the full result as JSON")
    p_off.add_argument("--no-cache", action="store_true",
                       help="do not write the headline to the state dir")
    p_off.set_defaults(func=cmd_offline)

    p_ab = sub.add_parser("ab", help="compare the A/B arms against budgeter outcomes")
    p_ab.add_argument("--since", metavar="YYYY-MM-DD",
                      help="only count budgeter rows on/after this date")
    p_ab.add_argument("--log", help="budgeter usage log path (default: budgeter's own)")
    p_ab.add_argument("--json", action="store_true", help="emit the summary as JSON")
    p_ab.set_defaults(func=cmd_ab)

    p_lab = sub.add_parser("labels", help="print the per-dimension label vocabulary")
    p_lab.add_argument("--json", action="store_true", help="emit the raw vocabulary")
    p_lab.set_defaults(func=cmd_labels)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.state_dir:
        os.environ[store.TARGET_STATE_DIR_ENV] = str(Path(args.state_dir).resolve())
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
