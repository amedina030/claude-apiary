#!/usr/bin/env python3
"""
Summary of claude-apis budgeter usage log.

Usage:
    python report.py                    # default: grouped by session > task
    python report.py --all              # include zero-delta entries
    python report.py --date 2026-03-14  # single date
    python report.py --since 2026-03-01 # from date onwards
    python report.py --flat             # flat list, no grouping
    python report.py --grouped          # group by session only (no task breakdown)
"""
import json
import argparse
from pathlib import Path
from datetime import datetime

BUDGETER_DIR = Path(__file__).parent
LOG_PATH = BUDGETER_DIR / "data" / "usage_log.jsonl"
FEEDBACK_PATH = BUDGETER_DIR / "data" / "feedback.jsonl"
CLARIFIER_COST_LOG = Path.home() / ".claude" / "clarifier-logs" / "cost.log"


def _parse_cost_field(line, key):
    for part in line.split(" | "):
        if part.startswith(f"{key}: "):
            return part[len(f"{key}: "):]
    return None


def load_clarifier_costs():
    """Return dict of session_id -> total clarifier tokens from cost.log."""
    if not CLARIFIER_COST_LOG.exists():
        return {}
    costs = {}
    for line in CLARIFIER_COST_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        sid = _parse_cost_field(line, "session_id")
        tokens_str = _parse_cost_field(line, "total_tokens")
        if sid and sid != "unknown" and tokens_str:
            costs[sid] = costs.get(sid, 0) + int(tokens_str)
    return costs


def load_feedback():
    if not FEEDBACK_PATH.exists():
        return []
    entries = []
    for line in FEEDBACK_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def load_entries():
    if not LOG_PATH.exists():
        return []
    entries = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def entry_date(e):
    return datetime.fromisoformat(e["timestamp"]).date()


def short_session(session_id):
    return session_id[:8] if session_id else "unknown"


def median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) // 2


def net_delta(e):
    """Return net_tokens_delta if present (new entries), else tokens_delta (old entries)."""
    return e.get("net_tokens_delta", e["tokens_delta"])


def print_summary(entries, sessions=None):
    total = sum(net_delta(e) for e in entries)
    count = len(entries)
    deltas = [net_delta(e) for e in entries]
    med_per_call = median(deltas) if deltas else 0

    by_tool = {}
    for e in entries:
        t = e.get("tool_name", "unknown")
        by_tool.setdefault(t, []).append(net_delta(e))

    print(f"{'TOTAL TOKENS':>24} {total:>12,}")
    print(f"{'TOTAL ENTRIES':>24} {count:>12,}")
    print(f"{'MEDIAN TOKENS/CALL':>24} {med_per_call:>12,}")
    if sessions is not None:
        sess_totals = [sum(net_delta(e) for e in s) for s in sessions.values()]
        med_per_session = median(sess_totals) if sess_totals else 0
        print(f"{'SESSIONS':>24} {len(sessions):>12,}")
        print(f"{'MEDIAN TOKENS/SESSION':>24} {med_per_session:>12,}")
    for tool, tool_deltas in sorted(by_tool.items()):
        med = median(tool_deltas)
        label = f"MEDIAN TOKENS/{tool.upper()}"
        print(f"  {label:<22} {med:>12,}")


def print_flat(entries):
    print(f"{'DATE':<12} {'TIME':<8} {'TOOL':<7} {'DELTA':>10}  MESSAGE")
    print("-" * 82)
    for e in entries:
        ts = e["timestamp"]
        d = ts[:10]
        t = ts[11:19]
        tool = e.get("tool_name", "")[:6]
        delta = net_delta(e)
        msg = e["assistant_message"][:52].replace("\n", " ")
        print(f"{d:<12} {t:<8} {tool:<7} {delta:>10,}  {msg}")
    print("-" * 82)
    print_summary(entries)


def print_by_turn(entries):
    # Group by session_id, then by task_turn (falls back to turn_number for old entries)
    sessions = {}
    for e in entries:
        sid = e.get("session_id", "unknown")
        sessions.setdefault(sid, []).append(e)

    clarifier_costs = load_clarifier_costs()

    all_task_totals = []
    for sid, sess_entries in sessions.items():
        sess_total = sum(net_delta(e) for e in sess_entries)
        first_ts = sess_entries[0]["timestamp"]
        d = first_ts[:10]
        t = first_ts[11:19]
        print(f"Session {short_session(sid)}  {d} {t}  ({sess_total:,} tokens)")
        clarifier_tokens = clarifier_costs.get(sid, 0)
        if clarifier_tokens > 0:
            main_tokens = sess_total - clarifier_tokens
            print(f"  [clarifier: {clarifier_tokens:,} tokens | main: {main_tokens:,} tokens]")

        tasks = {}
        for e in sess_entries:
            task = e.get("task_turn", e.get("turn_number", 0))
            tasks.setdefault(task, []).append(e)

        for task_num in sorted(tasks):
            task_entries = tasks[task_num]
            task_total = sum(net_delta(e) for e in task_entries)
            all_task_totals.append(task_total)
            t = task_entries[0]["timestamp"][11:19]
            user_turns = sorted(set(e.get("turn_number", task_num) for e in task_entries))
            turns_label = f"turns {user_turns[0]}-{user_turns[-1]}" if len(user_turns) > 1 else f"turn {user_turns[0]}"
            user_prompt = task_entries[0].get("user_message", "")
            print(f"  Task {task_num:<3}  {t}  {task_total:>10,} tokens  [{turns_label}]")
            if user_prompt:
                print(f"    > {user_prompt[:76].replace(chr(10), ' ')}")
            print(f"    {'TURN':<5} {'TOOL':<7} {'DELTA':>10}  MESSAGE")
            for e in task_entries:
                turn = e.get("turn_number", task_num)
                tool = e.get("tool_name", "")[:6]
                delta = net_delta(e)
                msg = e["assistant_message"][:44].replace("\n", " ")
                print(f"    {turn:<5} {tool:<7} {delta:>10,}  {msg}")
        print()

    print("-" * 82)
    total = sum(net_delta(e) for e in entries)
    med_per_task = median(all_task_totals) if all_task_totals else 0
    sess_totals = [sum(net_delta(e) for e in s) for s in sessions.values()]
    med_per_session = median(sess_totals) if sess_totals else 0
    print(f"{'TOTAL TOKENS':>24} {total:>12,}")
    print(f"{'TOTAL TASKS':>24} {len(all_task_totals):>12,}")
    print(f"{'MEDIAN TOKENS/TASK':>24} {med_per_task:>12,}")
    print(f"{'SESSIONS':>24} {len(sessions):>12,}")
    print(f"{'MEDIAN TOKENS/SESSION':>24} {med_per_session:>12,}")


def print_grouped(entries):
    # Group by session_id
    sessions = {}
    for e in entries:
        sid = e.get("session_id", "unknown")
        sessions.setdefault(sid, []).append(e)

    for sid, sess_entries in sessions.items():
        sess_total = sum(net_delta(e) for e in sess_entries)
        first_ts = sess_entries[0]["timestamp"]
        d = first_ts[:10]
        t = first_ts[11:19]
        print(f"Session {short_session(sid)}  {d} {t}  ({len(sess_entries)} calls, {sess_total:,} tokens)")
        print(f"  {'TIME':<8} {'TOOL':<7} {'DELTA':>10}  MESSAGE")
        print(f"  {'-' * 72}")
        for e in sess_entries:
            t = e["timestamp"][11:19]
            tool = e.get("tool_name", "")[:6]
            delta = net_delta(e)
            msg = e["assistant_message"][:52].replace("\n", " ")
            print(f"  {t:<8} {tool:<7} {delta:>10,}  {msg}")
        print()

    print("-" * 82)
    # Compute task totals for task-level summary stats
    all_task_totals = []
    for sess_entries in sessions.values():
        tasks = {}
        for e in sess_entries:
            task = e.get("task_turn", e.get("turn_number", 0))
            tasks.setdefault(task, []).append(e)
        for task_entries in tasks.values():
            all_task_totals.append(sum(net_delta(e) for e in task_entries))

    total = sum(net_delta(e) for e in entries)
    med_per_task = median(all_task_totals) if all_task_totals else 0
    sess_totals = [sum(net_delta(e) for e in s) for s in sessions.values()]
    med_per_session = median(sess_totals) if sess_totals else 0
    print(f"{'TOTAL TOKENS':>24} {total:>12,}")
    print(f"{'TOTAL TASKS':>24} {len(all_task_totals):>12,}")
    print(f"{'MEDIAN TOKENS/TASK':>24} {med_per_task:>12,}")
    print(f"{'SESSIONS':>24} {len(sessions):>12,}")
    print(f"{'MEDIAN TOKENS/SESSION':>24} {med_per_session:>12,}")


def _percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    idx = (p / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def print_feedback(log_entries, feedback_entries, expensive_percentile=75):
    if not feedback_entries:
        print("No feedback records yet.")
        return

    # Compute actual cost per task from the log
    task_costs = {}
    for e in log_entries:
        if "turn_number" not in e:
            continue
        task_turn = e.get("task_turn", e["turn_number"])
        key = (e.get("session_id", ""), task_turn)
        task_costs[key] = task_costs.get(key, 0) + net_delta(e)

    # Enrich feedback records with actual_cost
    records = []
    seen = set()
    for f in feedback_entries:
        key = (f.get("session_id", ""), f.get("task_turn", 0))
        if key in seen:
            continue  # deduplicate (PRE + Stop could both write for the same task)
        seen.add(key)
        actual = task_costs.get(key, 0)
        records.append({**f, "actual_cost": actual})

    if not records:
        print("No feedback records with matching log entries.")
        return

    # Compute expensive threshold from actual costs
    all_actual = [r["actual_cost"] for r in records]
    threshold = _percentile(all_actual, expensive_percentile)

    for r in records:
        r["was_expensive"] = r["actual_cost"] >= threshold

    flagged = [r for r in records if r.get("warning_fired")]
    unflagged = [r for r in records if not r.get("warning_fired")]
    true_pos = [r for r in flagged if r["was_expensive"]]
    false_pos = [r for r in flagged if not r["was_expensive"]]
    false_neg = [r for r in unflagged if r["was_expensive"]]

    precision = len(true_pos) / len(flagged) if flagged else 0.0

    print(f"FEEDBACK ANALYSIS  ({len(records)} tasks, {len(flagged)} flagged)\n")
    print(f"  {'Overall precision:':<28} {len(true_pos)}/{len(flagged)} ({precision:.0%})")
    print(f"  {'False positives:':<28} {len(false_pos)}")
    print(f"  {'Missed (expensive, no warn):':<28} {len(false_neg)}")
    print(f"  {'Expensive threshold:':<28} {expensive_percentile}th percentile = {threshold:,.0f} tokens")

    # Rule combination breakdown
    from collections import Counter
    combo_stats = {}
    for r in records:
        combo = tuple(sorted(r.get("scope_flags", [])))
        if combo not in combo_stats:
            combo_stats[combo] = {"tasks": 0, "expensive": 0, "total_actual": 0}
        combo_stats[combo]["tasks"] += 1
        if r["was_expensive"]:
            combo_stats[combo]["expensive"] += 1
        combo_stats[combo]["total_actual"] += r["actual_cost"]

    if combo_stats:
        print(f"\n  {'Rule combination':<42} {'Tasks':>6}  {'Prec':>6}  {'Avg actual':>12}")
        print(f"  {'-' * 70}")
        for combo, s in sorted(combo_stats.items(), key=lambda x: -x[1]["tasks"]):
            label = " + ".join(combo) if combo else "(no rules)"
            prec = s["expensive"] / s["tasks"] if s["tasks"] else 0
            avg = s["total_actual"] / s["tasks"] if s["tasks"] else 0
            flag = "ok" if prec >= 0.75 else ("~" if prec >= 0.5 else "x")
            print(f"  {label:<42} {s['tasks']:>6}  {prec:>5.0%}  {avg:>12,.0f}  {flag}")

    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Show only entries from this date (YYYY-MM-DD)")
    parser.add_argument("--since", help="Show entries from this date onwards (YYYY-MM-DD)")
    parser.add_argument("--flat", action="store_true", help="Flat list instead of session grouping")
    parser.add_argument("--grouped", action="store_true", help="Group by session only (no task breakdown)")
    parser.add_argument("--by-turn", action="store_true", help="Alias for default (session > task grouping)")
    parser.add_argument("--all", action="store_true", help="Include zero-delta entries")
    parser.add_argument("--feedback", action="store_true", help="Show warning precision and rule breakdown")
    args = parser.parse_args()

    if args.feedback:
        import json as _json
        try:
            config = _json.loads((BUDGETER_DIR / "config.json").read_text(encoding="utf-8"))
        except Exception:
            config = {}
        print_feedback(load_entries(), load_feedback(),
                       expensive_percentile=config.get("expensive_percentile_feedback", 75))
        return

    entries = load_entries()

    if not args.all:
        entries = [e for e in entries if e.get("tokens_delta", 0) != 0]

    if args.date:
        target = parse_date(args.date)
        entries = [e for e in entries if entry_date(e) == target]
    elif args.since:
        since = parse_date(args.since)
        entries = [e for e in entries if entry_date(e) >= since]

    if not entries:
        print("No entries found.")
        return

    if args.flat:
        print_flat(entries)
    elif args.grouped:
        print_grouped(entries)
    else:
        print_by_turn(entries)


if __name__ == "__main__":
    main()
