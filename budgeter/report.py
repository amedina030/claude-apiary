#!/usr/bin/env python3
"""
Summary of claude-apis budgeter usage log.

Usage:
    python report.py                    # non-zero entries, grouped by session
    python report.py --all              # include zero-delta entries
    python report.py --date 2026-03-14  # single date
    python report.py --since 2026-03-01 # from date onwards
    python report.py --flat             # flat list, no session grouping
    python report.py --by-turn          # grouped by session > task (chains continuation turns)
"""
import json
import argparse
from pathlib import Path
from datetime import datetime

BUDGETER_DIR = Path(__file__).parent
LOG_PATH = BUDGETER_DIR / "data" / "usage_log.jsonl"


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

    all_task_totals = []
    for sid, sess_entries in sessions.items():
        sess_total = sum(net_delta(e) for e in sess_entries)
        first_ts = sess_entries[0]["timestamp"]
        d = first_ts[:10]
        t = first_ts[11:19]
        print(f"Session {short_session(sid)}  {d} {t}  ({sess_total:,} tokens)")

        tasks = {}
        for e in sess_entries:
            task = e.get("task_turn", e.get("turn_number", 0))
            tasks.setdefault(task, []).append(e)

        for task_num in sorted(tasks):
            task_entries = tasks[task_num]
            task_total = sum(net_delta(e) for e in task_entries)
            all_task_totals.append(task_total)
            first_msg = task_entries[0].get("assistant_message", "")[:52].replace("\n", " ")
            t = task_entries[0]["timestamp"][11:19]
            user_turns = sorted(set(e.get("turn_number", task_num) for e in task_entries))
            turns_label = f"turns {user_turns[0]}-{user_turns[-1]}" if len(user_turns) > 1 else f"turn {user_turns[0]}"
            print(f"  Task {task_num:<3}  {t}  {task_total:>10,} tokens  [{turns_label}]  {first_msg}")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Show only entries from this date (YYYY-MM-DD)")
    parser.add_argument("--since", help="Show entries from this date onwards (YYYY-MM-DD)")
    parser.add_argument("--flat", action="store_true", help="Flat list instead of session grouping")
    parser.add_argument("--by-turn", action="store_true", help="Group by session and user turn")
    parser.add_argument("--all", action="store_true", help="Include zero-delta entries")
    args = parser.parse_args()

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
    elif args.by_turn:
        print_by_turn(entries)
    else:
        print_grouped(entries)


if __name__ == "__main__":
    main()
