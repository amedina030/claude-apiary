#!/usr/bin/env python3
"""
Round counter for the harden skill.
Tracks attack-defend round count per session.

Usage:
    round_counter.py start --session-id <id>   # initialize counter at 0
    round_counter.py tick --session-id <id>     # increment, print new count
    round_counter.py reset --session-id <id>    # reset to 0
    round_counter.py status --session-id <id>   # print count without incrementing
"""
import argparse
import json
import os
import sys
from pathlib import Path

TMP_DIR = Path(os.environ.get("HARDEN_TMP_DIR", Path(__file__).parent.resolve() / "tmp"))


def _state_path(session_id: str) -> Path:
    safe = session_id.replace("/", "-").replace("\\", "-")
    return TMP_DIR / f"round_{safe}.json"


def _read(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("count", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def _write(path: Path, session_id: str, count: int) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"session_id": session_id, "count": count}) + "\n",
        encoding="utf-8",
    )


def cmd_start(session_id: str) -> None:
    _write(_state_path(session_id), session_id, 0)
    print("0")


def cmd_tick(session_id: str) -> None:
    path = _state_path(session_id)
    count = _read(path) + 1
    _write(path, session_id, count)
    print(count)


def cmd_reset(session_id: str) -> None:
    _write(_state_path(session_id), session_id, 0)
    print("0")


def cmd_status(session_id: str) -> None:
    print(_read(_state_path(session_id)))


def main():
    parser = argparse.ArgumentParser(description="Harden round counter")
    sub = parser.add_subparsers(dest="command")

    for name in ("start", "tick", "reset", "status"):
        p = sub.add_parser(name)
        p.add_argument("--session-id", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"start": cmd_start, "tick": cmd_tick, "reset": cmd_reset, "status": cmd_status}[
        args.command
    ](args.session_id)


if __name__ == "__main__":
    main()
