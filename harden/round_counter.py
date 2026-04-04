#!/usr/bin/env python3
"""
Round counter for the harden skill.
Tracks attack-defend round count and defender agent ID per session.

Usage:
    round_counter.py start --session-id <id>   # initialize counter at 0
    round_counter.py tick --session-id <id>     # increment, print new count
    round_counter.py reset --session-id <id>    # reset to 0
    round_counter.py status --session-id <id>   # print count without incrementing
    round_counter.py defender --session-id <id> --set <agent_id>  # store defender ID
    round_counter.py defender --session-id <id> --get             # retrieve defender ID
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


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state) + "\n", encoding="utf-8")


def cmd_start(session_id: str) -> None:
    _write_state(_state_path(session_id), {"session_id": session_id, "count": 0})
    print("0")


def cmd_tick(session_id: str) -> None:
    path = _state_path(session_id)
    state = _read_state(path)
    count = state.get("count", 0) + 1
    state.update({"session_id": session_id, "count": count})
    _write_state(path, state)
    print(count)


def cmd_reset(session_id: str) -> None:
    _write_state(_state_path(session_id), {"session_id": session_id, "count": 0})
    print("0")


def cmd_status(session_id: str) -> None:
    print(_read_state(_state_path(session_id)).get("count", 0))


def cmd_defender(session_id: str, set_id: str = None) -> None:
    path = _state_path(session_id)
    state = _read_state(path)
    if set_id is not None:
        state.update({"session_id": session_id, "defender_agent_id": set_id})
        _write_state(path, state)
        print(set_id)
    else:
        agent_id = state.get("defender_agent_id")
        if agent_id is None:
            print("ERROR: no defender_agent_id in state", file=sys.stderr)
            sys.exit(1)
        print(agent_id)


def main():
    parser = argparse.ArgumentParser(description="Harden round counter")
    sub = parser.add_subparsers(dest="command")

    for name in ("start", "tick", "reset", "status"):
        p = sub.add_parser(name)
        p.add_argument("--session-id", required=True)

    p_def = sub.add_parser("defender")
    p_def.add_argument("--session-id", required=True)
    group = p_def.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", dest="set_id", help="Store defender agent ID")
    group.add_argument("--get", action="store_true", help="Retrieve defender agent ID")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "defender":
        cmd_defender(args.session_id, set_id=args.set_id)
    else:
        {"start": cmd_start, "tick": cmd_tick, "reset": cmd_reset, "status": cmd_status}[
            args.command
        ](args.session_id)


if __name__ == "__main__":
    main()
