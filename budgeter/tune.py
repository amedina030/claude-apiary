#!/usr/bin/env python3
"""
tune.py v1 — Suggest rule weight adjustments based on feedback precision.

Loads feedback.jsonl + usage_log.jsonl, computes per-rule precision, and
proposes weight changes proportional to each rule's relative precision.
Requires confirmation before writing to config.json.

Usage:
    python tune.py                  # analyze and suggest
    python tune.py --min N          # min samples per rule before suggesting (default: 5)
    python tune.py --percentile N   # expensive threshold percentile (default: from config)
    python tune.py --yes            # skip confirmation prompt (write immediately)
"""
import json
import sys
import argparse
import tempfile
import os
from pathlib import Path

BUDGETER_DIR = Path(__file__).parent
CONFIG_PATH = BUDGETER_DIR / "config.json"
LOG_PATH = BUDGETER_DIR / "data" / "usage_log.jsonl"
FEEDBACK_PATH = BUDGETER_DIR / "data" / "feedback.jsonl"

WEIGHT_MIN = 0.3
WEIGHT_MAX = 2.5


_MAX_LOG_BYTES = 100 * 1024 * 1024  # 100 MB — prevents OOM on runaway log files


def load_jsonl(path):
    if not path.exists():
        return []
    try:
        if path.stat().st_size > _MAX_LOG_BYTES:
            print(f"Warning: {path.name} exceeds {_MAX_LOG_BYTES // (1024*1024)} MB — skipping to avoid OOM.",
                  file=sys.stderr)
            return []
    except OSError:
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    idx = (p / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def compute_actual_costs(log_entries):
    """Return dict of (session_id, task_turn) -> total net tokens."""
    costs = {}
    for e in log_entries:
        if "turn_number" not in e:
            continue
        task_turn = e.get("task_turn", e["turn_number"])
        key = (e.get("session_id", ""), task_turn)
        delta = e.get("net_tokens_delta", e.get("tokens_delta", 0))
        costs[key] = costs.get(key, 0) + delta
    return costs


def enrich_feedback(log_entries, feedback_entries, expensive_percentile):
    """
    Join feedback with log costs, compute actual_cost and was_expensive per task.
    Deduplicates feedback entries (PRE + Stop can both write for the same task).
    Returns enriched records and the expensive threshold.
    """
    actual_costs = compute_actual_costs(log_entries)

    records = []
    seen = set()
    for f in feedback_entries:
        key = (f.get("session_id", ""), f.get("task_turn", 0))
        if key in seen:
            continue
        seen.add(key)
        actual = actual_costs.get(key, 0)
        records.append({**f, "actual_cost": actual})

    if not records:
        return records, 0

    all_actual = [r["actual_cost"] for r in records]
    threshold = _percentile(all_actual, expensive_percentile)
    for r in records:
        r["was_expensive"] = r["actual_cost"] >= threshold

    return records, threshold


def analyze_rules(records, min_samples):
    """
    For each rule name seen in any scope_flags, compute precision stats.
    Returns dict: rule -> {fires, expensive, precision, sufficient}.
    """
    all_rules = set()
    for r in records:
        all_rules.update(r.get("scope_flags", []))

    stats = {}
    for rule in sorted(all_rules):
        fired = [r for r in records if rule in r.get("scope_flags", [])]
        expensive = [r for r in fired if r["was_expensive"]]
        stats[rule] = {
            "fires": len(fired),
            "expensive": len(expensive),
            "precision": len(expensive) / len(fired) if fired else 0.0,
            "sufficient": len(fired) >= min_samples,
        }
    return stats


def propose_weights(current_weights, rule_stats):
    """
    Scale each rule's weight proportionally to its precision relative to
    the mean precision across all rules with sufficient data.
    Returns (proposed_weights dict, mean_precision).
    Only rules with sufficient data appear in proposed_weights.
    """
    eligible = {rule: s for rule, s in rule_stats.items() if s["sufficient"]}

    if not eligible:
        return {}, 0.0

    mean_precision = sum(s["precision"] for s in eligible.values()) / len(eligible)
    if mean_precision == 0:
        return {}, 0.0

    proposed = {}
    for rule, s in eligible.items():
        current = current_weights.get(rule, 1.0)
        if s["precision"] == 0.0:
            # Rule never predicted correctly — set to WEIGHT_MIN so it
            # cannot trigger a warning on its own but still participates
            # in multi-rule score sums at the stated minimum weight.
            # Using WEIGHT_MIN (not WEIGHT_MIN/10) keeps this branch
            # consistent with the clamp applied in the normal branch.
            new_weight = WEIGHT_MIN
        else:
            new_weight = current * (s["precision"] / mean_precision)
            new_weight = max(WEIGHT_MIN, min(WEIGHT_MAX, round(new_weight, 1)))
        proposed[rule] = new_weight

    return proposed, mean_precision


def print_report(records, threshold, rule_stats, current_weights, proposed_weights,
                 mean_precision, expensive_percentile, min_samples):
    n_flagged = sum(1 for r in records if r.get("scope_flags"))
    n_expensive = sum(1 for r in records if r["was_expensive"])
    flagged_expensive = sum(1 for r in records if r.get("scope_flags") and r["was_expensive"])
    overall_prec = flagged_expensive / n_flagged if n_flagged else 0.0

    print("TUNE.PY v1 — Weight Adjustment Suggestions")
    print("=" * 52)
    print()
    print(f"  Tasks analyzed  : {len(records)}")
    print(f"  Flagged tasks   : {n_flagged}")
    print(f"  Expensive tasks : {n_expensive}  "
          f"({expensive_percentile}th percentile = {threshold:,.0f} tokens)")
    print(f"  Overall flagged precision: {flagged_expensive}/{n_flagged} = {overall_prec:.0%}")
    print()

    print(f"  Per-rule analysis  (min {min_samples} samples required for suggestion)")
    print(f"  {'Rule':<22} {'Fires':>6}  {'Exp':>5}  {'Prec':>6}  {'Note'}")
    print(f"  {'-' * 58}")
    for rule, s in rule_stats.items():
        note = "" if s["sufficient"] else f"[need {min_samples}, have {s['fires']}]"
        print(f"  {rule:<22} {s['fires']:>6}  {s['expensive']:>5}  {s['precision']:>5.0%}  {note}")
    print()

    if not proposed_weights:
        print("  No rules have sufficient data for weight suggestions yet.")
        print(f"  Run more sessions and check again when rules have ≥ {min_samples} samples.")
        return False

    print(f"  Mean precision (eligible rules): {mean_precision:.0%}")
    print()

    any_change = False
    print(f"  Proposed weight changes:")
    print(f"  {'Rule':<22} {'Current':>8}  {'Proposed':>9}  {'Change':>8}  Reason")
    print(f"  {'-' * 70}")
    for rule in sorted(set(list(current_weights.keys()) + list(proposed_weights.keys()))):
        current = current_weights.get(rule, 1.0)
        if rule in proposed_weights:
            new = proposed_weights[rule]
            delta = new - current
            sign = "+" if delta >= 0 else ""
            prec = rule_stats[rule]["precision"]
            direction = "above mean" if prec > mean_precision else ("at mean" if prec == mean_precision else "below mean")
            print(f"  {rule:<22} {current:>8.1f}  {new:>9.1f}  {sign}{delta:>+.1f}     {prec:.0%} precision ({direction})")
            if new != current:
                any_change = True
        else:
            s = rule_stats.get(rule)
            if s:
                note = f"[insufficient data: {s['fires']} fires]"
            else:
                note = "[no data]"
            print(f"  {rule:<22} {current:>8.1f}  {'—':>9}   —        {note}")
    print()

    return any_change


def main():
    parser = argparse.ArgumentParser(description="Suggest budgeter rule weight adjustments")
    parser.add_argument("--min", type=int, default=5, metavar="N",
                        help="Min samples per rule before suggesting changes (default: 5)")
    parser.add_argument("--percentile", type=int, default=None, metavar="N",
                        help="Expensive threshold percentile (default: from config)")
    parser.add_argument("--yes", action="store_true",
                        help="Apply changes without confirmation prompt")
    args = parser.parse_args()

    # Load config
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Error: config not found at {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    expensive_percentile = args.percentile or config.get("expensive_percentile_feedback", 75)
    current_weights = config.get("rule_weights", {})

    # Load data
    log_entries = load_jsonl(LOG_PATH)
    feedback_entries = load_jsonl(FEEDBACK_PATH)

    if not feedback_entries:
        print("No feedback data yet. Run some sessions with budgeter-log enabled first.")
        sys.exit(0)

    # Enrich and analyze
    records, threshold = enrich_feedback(log_entries, feedback_entries, expensive_percentile)

    if not records:
        print("No feedback records could be matched to log entries.")
        sys.exit(0)

    rule_stats = analyze_rules(records, args.min)
    proposed_weights, mean_precision = propose_weights(current_weights, rule_stats)

    # Print report
    any_change = print_report(
        records, threshold, rule_stats, current_weights, proposed_weights,
        mean_precision, expensive_percentile, args.min
    )

    if not any_change:
        print("  No weight changes to apply.")
        return

    # Confirm and write
    if not args.yes:
        try:
            answer = input("Apply these changes to config.json? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return
        if answer != "y":
            print("No changes made.")
            return

    # Merge proposed weights into config (preserve unchanged rules)
    new_weights = {**current_weights, **proposed_weights}
    config["rule_weights"] = new_weights
    # Write atomically: write to a temp file in the same directory, then rename
    # so a crash mid-write never truncates the live config.
    config_dir = CONFIG_PATH.parent
    try:
        fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp", prefix="config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tf:
                tf.write(json.dumps(config, indent=2) + "\n")
            os.replace(tmp_path, CONFIG_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        print(f"Error writing config: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated config.json with new rule weights.")


if __name__ == "__main__":
    main()
