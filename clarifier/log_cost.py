#!/usr/bin/env python3
"""
Track and log clarifier session costs.

Usage:
  python log_cost.py tally --id <uuid> --tokens N --tools N --duration N
      Accumulate costs for one clarifier Agent invocation.
      Call this after every clarifier spawn or resume.

  python log_cost.py finalize --id <uuid> --log <filename> --prompt "<text>"
      Write the final cost entry to cost.log using summed totals.
      Cleans up the tally file.

Tally files are stored at ~/.claude/clarifier-logs/.tally-<uuid>.json.
Cost log is written to ~/.claude/clarifier-logs/cost.log.
"""

import argparse
import datetime
import json
from pathlib import Path


def default_logs_dir() -> Path:
    return Path.home() / ".claude" / "clarifier-logs"


def tally_path(logs_dir: Path, session_id: str) -> Path:
    return logs_dir / f".tally-{session_id}.json"


def detect_session_id(budgeter_tmp: Path = None) -> str:
    """Auto-detect Claude session ID from budgeter tmp baseline file."""
    search_dir = budgeter_tmp if budgeter_tmp else Path.cwd() / "budgeter" / "tmp"
    if search_dir.is_dir():
        files = list(search_dir.glob("*_baseline.json"))
        if files:
            return files[0].name.split("_baseline.json")[0]
    return "unknown"


def cmd_tally(args):
    logs_dir = Path(args.logs_dir) if args.logs_dir else default_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = tally_path(logs_dir, args.id)

    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {
            "id": args.id,
            "total_tokens": 0,
            "total_tools": 0,
            "total_duration_ms": 0,
            "calls": 0,
        }

    data["total_tokens"] += args.tokens
    data["total_tools"] += args.tools
    data["total_duration_ms"] += args.duration
    data["calls"] += 1

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(
        f"Tally updated: {data['total_tokens']} tokens, "
        f"{data['total_tools']} tools, {data['calls']} call(s)"
    )


def cmd_finalize(args):
    logs_dir = Path(args.logs_dir) if args.logs_dir else default_logs_dir()
    path = tally_path(logs_dir, args.id)

    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"total_tokens": 0, "total_tools": 0, "total_duration_ms": 0}

    if args.session_id:
        session_id = args.session_id
    else:
        budgeter_tmp = Path(args.budgeter_tmp) if args.budgeter_tmp else None
        session_id = detect_session_id(budgeter_tmp)

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt_snippet = args.prompt[:80].replace('"', "'")

    entry = (
        f'{ts} | id: {args.id} | session_id: {session_id} | '
        f'total_tokens: {data["total_tokens"]} | '
        f'tool_uses: {data["total_tools"]} | duration_ms: {data["total_duration_ms"]} | '
        f'log: {args.log} | prompt: "{prompt_snippet}"'
    )

    logs_dir.mkdir(parents=True, exist_ok=True)
    cost_log = logs_dir / "cost.log"
    with open(cost_log, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

    if path.exists():
        path.unlink()

    print(f"Cost logged: {entry}")


def main():
    parser = argparse.ArgumentParser(description="Track clarifier session costs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tally_p = subparsers.add_parser("tally", help="Accumulate costs for one invocation")
    tally_p.add_argument("--id", required=True, help="Clarifier session UUID")
    tally_p.add_argument("--tokens", type=int, required=True, help="Total tokens this call")
    tally_p.add_argument("--tools", type=int, required=True, help="Tool uses this call")
    tally_p.add_argument("--duration", type=int, required=True, help="Duration ms this call")
    tally_p.add_argument("--logs-dir", help="Override default logs directory (for testing)")

    fin_p = subparsers.add_parser("finalize", help="Write cost.log entry and clean up")
    fin_p.add_argument("--id", required=True, help="Clarifier session UUID")
    fin_p.add_argument("--log", required=True, help="Log filename (e.g. clarifier-2026-03-14-214013-a3f2.md)")
    fin_p.add_argument("--prompt", required=True, help="Original prompt text")
    fin_p.add_argument("--session-id", help="Claude session ID (overrides auto-detection)")
    fin_p.add_argument("--budgeter-tmp", help="Path to budgeter/tmp for session_id auto-detection (for testing)")
    fin_p.add_argument("--logs-dir", help="Override default logs directory (for testing)")

    args = parser.parse_args()

    if args.command == "tally":
        cmd_tally(args)
    elif args.command == "finalize":
        cmd_finalize(args)


if __name__ == "__main__":
    main()
