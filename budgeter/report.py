#!/usr/bin/env python3
"""
Summary of claude-apiary budgeter usage log.

Usage:
    python report.py                    # default: grouped by session > task
    python report.py --all              # include zero-delta entries
    python report.py --date 2026-03-14  # single date
    python report.py --since 2026-03-01 # from date onwards
    python report.py --flat             # flat list, no grouping
    python report.py --grouped          # group by session only (no task breakdown)
    python report.py --by-agent         # per-agent-type token breakdown
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BUDGETER_DIR = Path(__file__).parent
LOG_PATH = BUDGETER_DIR / "data" / "usage_log.jsonl"

# 100 MB — generous cap for local log files; prevents OOM on runaway logs
_MAX_LOG_BYTES = 100 * 1024 * 1024


def _read_jsonl(path):
    """Read a JSONL file into a list of dicts, skipping invalid lines.
    Returns an empty list when the file does not exist or exceeds _MAX_LOG_BYTES."""
    if not path.exists():
        return []
    try:
        if path.stat().st_size > _MAX_LOG_BYTES:
            print(
                f"Warning: {path.name} exceeds {_MAX_LOG_BYTES // (1024 * 1024)} MB — skipping to avoid OOM.",
                file=sys.stderr,
            )
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


def load_entries():
    return _read_jsonl(LOG_PATH)


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def entry_date(e):
    try:
        return datetime.fromisoformat(e["timestamp"]).date()
    except (ValueError, KeyError):
        return None


def short_session(session_id):
    try:
        from core.session import SessionId

        return SessionId(session_id).short
    except (ValueError, ImportError):
        return session_id[:8] if session_id else "unknown"


def median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) // 2


def net_delta(e):
    """Return net_tokens_delta if present (new entries), else tokens_delta (old entries)."""
    return e.get("net_tokens_delta", e["tokens_delta"])


# Pricing weights relative to regular input tokens (Opus rates as baseline).
# input: $15/MTok, cache_read: $1.50/MTok (10%), cache_write: 1.25x,
# output: $75/MTok (5x). Overridable via config.json keys:
#   price_weight_input, price_weight_cache, price_weight_cache_creation,
#   price_weight_output
_DEFAULT_PRICE_WEIGHT_INPUT = 1.0
_DEFAULT_PRICE_WEIGHT_CACHE = 0.1
_DEFAULT_PRICE_WEIGHT_CACHE_CREATION = 1.25
_DEFAULT_PRICE_WEIGHT_OUTPUT = 5.0


def _load_price_weights():
    """Load pricing weights from config.json, falling back to module defaults."""
    try:
        config = json.loads((BUDGETER_DIR / "config.json").read_text(encoding="utf-8"))
        return (
            config.get("price_weight_input", _DEFAULT_PRICE_WEIGHT_INPUT),
            config.get("price_weight_cache", _DEFAULT_PRICE_WEIGHT_CACHE),
            config.get("price_weight_cache_creation", _DEFAULT_PRICE_WEIGHT_CACHE_CREATION),
            config.get("price_weight_output", _DEFAULT_PRICE_WEIGHT_OUTPUT),
        )
    except Exception:
        return (
            _DEFAULT_PRICE_WEIGHT_INPUT,
            _DEFAULT_PRICE_WEIGHT_CACHE,
            _DEFAULT_PRICE_WEIGHT_CACHE_CREATION,
            _DEFAULT_PRICE_WEIGHT_OUTPUT,
        )


_price_weights_cache = None


def _get_price_weights():
    """Return cached pricing weights, loading from config.json only once per process."""
    global _price_weights_cache
    if _price_weights_cache is None:
        _price_weights_cache = _load_price_weights()
    return _price_weights_cache


def weighted_delta(e):
    """Return weighted token count, scaling cache and output relative to input.
    Weights are loaded from config.json once per process (cached) so pricing
    changes take effect on the next invocation without repeated file I/O."""
    w_input, w_cache, w_create, w_output = _get_price_weights()
    inp = e.get("input_tokens_delta", 0)
    cache = e.get("cache_tokens_delta", 0)
    create = e.get("cache_creation_tokens_delta", 0)
    out = e.get("output_tokens_delta", 0)
    if inp or cache or create or out:
        return int(inp * w_input + cache * w_cache + create * w_create + out * w_output)
    # Fallback for old entries without split fields
    return net_delta(e)


def print_summary(entries, sessions=None, weighted=False):
    val = weighted_delta if weighted else net_delta
    label_suffix = " (weighted)" if weighted else ""
    total = sum(val(e) for e in entries)
    count = len(entries)
    deltas = [val(e) for e in entries]
    med_per_call = median(deltas) if deltas else 0

    by_tool = {}
    for e in entries:
        t = e.get("tool_name", "unknown")
        by_tool.setdefault(t, []).append(e)

    print(f"{'TOTAL TOKENS' + label_suffix:>30} {total:>12,}")
    print(f"{'TOTAL ENTRIES':>30} {count:>12,}")
    print(f"{'MEDIAN TOKENS/CALL':>30} {med_per_call:>12,}")
    if sessions is not None:
        sess_totals = [sum(val(e) for e in s) for s in sessions.values()]
        med_per_session = median(sess_totals) if sess_totals else 0
        print(f"{'SESSIONS':>30} {len(sessions):>12,}")
        print(f"{'MEDIAN TOKENS/SESSION':>30} {med_per_session:>12,}")
    for tool, tool_entries in sorted(by_tool.items()):
        tool_deltas = [val(e) for e in tool_entries]
        med = median(tool_deltas)
        label = f"MEDIAN TOKENS/{tool.upper()}"
        print(f"  {label:<28} {med:>12,}")


def print_flat(entries, weighted=False):
    val = weighted_delta if weighted else net_delta
    print(f"{'DATE':<12} {'TIME':<8} {'TOOL':<7} {'DELTA':>10}  MESSAGE")
    print("-" * 82)
    for e in entries:
        ts = e["timestamp"]
        d = ts[:10]
        t = ts[11:19]
        tool = e.get("tool_name", "")[:6]
        delta = val(e)
        msg = e["assistant_message"][:52].replace("\n", " ")
        print(f"{d:<12} {t:<8} {tool:<7} {delta:>10,}  {msg}")
    print("-" * 82)
    print_summary(entries, weighted=weighted)


def print_by_turn(entries, weighted=False):
    val = weighted_delta if weighted else net_delta
    # Group by session_id, then by task_turn (falls back to turn_number for old entries)
    sessions = {}
    for e in entries:
        sid = e.get("session_id", "unknown")
        sessions.setdefault(sid, []).append(e)

    all_task_totals = []
    for sid, sess_entries in sessions.items():
        sess_total = sum(val(e) for e in sess_entries)
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
            task_total = sum(val(e) for e in task_entries)
            all_task_totals.append(task_total)
            t = task_entries[0]["timestamp"][11:19]
            user_turns = sorted(set(e.get("turn_number", task_num) for e in task_entries))
            turns_label = (
                f"turns {user_turns[0]}-{user_turns[-1]}"
                if len(user_turns) > 1
                else f"turn {user_turns[0]}"
            )
            user_prompt = task_entries[0].get("user_message", "")
            print(f"  Task {task_num:<3}  {t}  {task_total:>10,} tokens  [{turns_label}]")
            if user_prompt:
                print(f"    > {user_prompt[:76].replace(chr(10), ' ')}")
            print(f"    {'TURN':<5} {'TOOL':<7} {'DELTA':>10}  MESSAGE")
            for e in task_entries:
                turn = e.get("turn_number", task_num)
                tool = e.get("tool_name", "")[:6]
                delta = val(e)
                msg = e["assistant_message"][:44].replace("\n", " ")
                print(f"    {turn:<5} {tool:<7} {delta:>10,}  {msg}")
        print()

    print("-" * 82)
    total = sum(val(e) for e in entries)
    med_per_task = median(all_task_totals) if all_task_totals else 0
    sess_totals = [sum(val(e) for e in s) for s in sessions.values()]
    med_per_session = median(sess_totals) if sess_totals else 0
    print(f"{'TOTAL TOKENS':>24} {total:>12,}")
    print(f"{'TOTAL TASKS':>24} {len(all_task_totals):>12,}")
    print(f"{'MEDIAN TOKENS/TASK':>24} {med_per_task:>12,}")
    print(f"{'SESSIONS':>24} {len(sessions):>12,}")
    print(f"{'MEDIAN TOKENS/SESSION':>24} {med_per_session:>12,}")


def print_grouped(entries, weighted=False):
    val = weighted_delta if weighted else net_delta
    # Group by session_id
    sessions = {}
    for e in entries:
        sid = e.get("session_id", "unknown")
        sessions.setdefault(sid, []).append(e)

    for sid, sess_entries in sessions.items():
        sess_total = sum(val(e) for e in sess_entries)
        first_ts = sess_entries[0]["timestamp"]
        d = first_ts[:10]
        t = first_ts[11:19]
        print(
            f"Session {short_session(sid)}  {d} {t}  ({len(sess_entries)} calls, {sess_total:,} tokens)"
        )
        print(f"  {'TIME':<8} {'TOOL':<7} {'DELTA':>10}  MESSAGE")
        print(f"  {'-' * 72}")
        for e in sess_entries:
            t = e["timestamp"][11:19]
            tool = e.get("tool_name", "")[:6]
            delta = val(e)
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
            all_task_totals.append(sum(val(e) for e in task_entries))

    total = sum(val(e) for e in entries)
    med_per_task = median(all_task_totals) if all_task_totals else 0
    sess_totals = [sum(val(e) for e in s) for s in sessions.values()]
    med_per_session = median(sess_totals) if sess_totals else 0
    print(f"{'TOTAL TOKENS':>24} {total:>12,}")
    print(f"{'TOTAL TASKS':>24} {len(all_task_totals):>12,}")
    print(f"{'MEDIAN TOKENS/TASK':>24} {med_per_task:>12,}")
    print(f"{'SESSIONS':>24} {len(sessions):>12,}")
    print(f"{'MEDIAN TOKENS/SESSION':>24} {med_per_session:>12,}")


def _agent_type(e):
    """Extract agent type from a log entry. Uses agent_type field if present,
    falls back to parsing [background] prefix from assistant_message."""
    at = e.get("agent_type", "")
    if at:
        return at
    msg = e.get("assistant_message", "")
    if msg.startswith("[background] "):
        return msg[len("[background] ") :]
    return "unknown"


def print_by_agent(entries, weighted=False):
    val = weighted_delta if weighted else net_delta
    agent_entries = [e for e in entries if e.get("tool_name") == "Agent"]
    if not agent_entries:
        print("No Agent entries found.")
        return

    by_type = {}
    for e in agent_entries:
        atype = _agent_type(e)
        by_type.setdefault(atype, []).append(e)

    print(f"{'AGENT TYPE':<30} {'CALLS':>6} {'TOTAL':>12} {'MEDIAN':>12} {'MAX':>12}")
    print("-" * 76)
    for atype in sorted(by_type, key=lambda t: -sum(val(e) for e in by_type[t])):
        items = by_type[atype]
        deltas = [val(e) for e in items]
        total = sum(deltas)
        med = median(deltas)
        mx = max(deltas)
        print(f"  {atype:<28} {len(items):>6} {total:>12,} {med:>12,} {mx:>12,}")

    print("-" * 76)
    all_deltas = [val(e) for e in agent_entries]
    print(
        f"  {'TOTAL':<28} {len(agent_entries):>6} {sum(all_deltas):>12,} {median(all_deltas):>12,} {max(all_deltas):>12,}"
    )
    print()

    # Non-agent entries summary
    non_agent = [e for e in entries if e.get("tool_name") != "Agent"]
    if non_agent:
        non_total = sum(val(e) for e in non_agent)
        print(f"  Non-agent tool calls: {len(non_agent)} ({non_total:,} tokens)")
        print(f"  Grand total: {sum(val(e) for e in entries):,} tokens")


def print_by_request(entries, weighted=False):
    """Group entries by the optional request_id field. Entries without a
    request_id are bucketed into '(no request)' so the user can see what
    fraction of activity is unattributed."""
    val = weighted_delta if weighted else net_delta

    by_req = {}
    for e in entries:
        rid = e.get("request_id") or "(no request)"
        by_req.setdefault(rid, []).append(e)

    if not by_req:
        print("No entries found.")
        return

    print(f"{'REQUEST ID':<40} {'CALLS':>6} {'TOTAL':>14} {'MEDIAN':>12} {'MAX':>12}")
    print("-" * 90)
    # Sort by total tokens desc; "(no request)" naturally falls wherever its size puts it
    for rid in sorted(by_req, key=lambda r: -sum(val(e) for e in by_req[r])):
        items = by_req[rid]
        deltas = [val(e) for e in items]
        total = sum(deltas)
        med = median(deltas)
        mx = max(deltas)
        rid_display = rid if len(rid) <= 38 else rid[:35] + "..."
        print(f"  {rid_display:<38} {len(items):>6} {total:>14,} {med:>12,} {mx:>12,}")

    print("-" * 90)
    all_deltas = [val(e) for e in entries]
    print(
        f"  {'TOTAL':<38} {len(entries):>6} {sum(all_deltas):>14,} {median(all_deltas):>12,} {max(all_deltas):>12,}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Show only entries from this date (YYYY-MM-DD)")
    parser.add_argument("--since", help="Show entries from this date onwards (YYYY-MM-DD)")
    parser.add_argument("--flat", action="store_true", help="Flat list instead of session grouping")
    parser.add_argument(
        "--grouped", action="store_true", help="Group by session only (no task breakdown)"
    )
    parser.add_argument(
        "--by-turn", action="store_true", help="Alias for default (session > task grouping)"
    )
    parser.add_argument(
        "--by-agent", action="store_true", help="Show per-agent-type token breakdown"
    )
    parser.add_argument(
        "--by-request",
        action="store_true",
        help="Group by request_id (sums multi-call chains like one runner run)",
    )
    parser.add_argument("--all", action="store_true", help="Include zero-delta entries")
    parser.add_argument(
        "--weighted",
        action="store_true",
        help="Weight tokens by type: cache 0.1x, output 5x (relative to input)",
    )
    args = parser.parse_args()

    entries = load_entries()

    if not args.all:
        entries = [e for e in entries if e.get("tokens_delta", 0) != 0]

    if args.date:
        target = parse_date(args.date)
        entries = [e for e in entries if entry_date(e) == target]
    elif args.since:
        since = parse_date(args.since)
        # Entries without a parseable timestamp are excluded rather than
        # incorrectly treated as matching (entry_date returns None for them).
        entries = [e for e in entries if entry_date(e) is not None and entry_date(e) >= since]

    if not entries:
        print("No entries found.")
        return

    if args.by_request:
        print_by_request(entries, weighted=args.weighted)
    elif args.by_agent:
        print_by_agent(entries, weighted=args.weighted)
    elif args.flat:
        print_flat(entries, weighted=args.weighted)
    elif args.grouped:
        print_grouped(entries, weighted=args.weighted)
    else:
        print_by_turn(entries, weighted=args.weighted)


if __name__ == "__main__":
    main()
