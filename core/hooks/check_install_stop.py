#!/usr/bin/env python3
"""
Stop hook — cleans up session flag files written by core PreToolUse hooks
(check_install.py, inject_session.py).
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.session import SessionId

FLAG_SUFFIXES = ["install_checked", "session_injected", "startup_done"]


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    raw_id = payload.get("session_id", "")
    if raw_id:
        try:
            sid = SessionId(raw_id)
        except ValueError:
            sys.exit(0)
        for suffix in FLAG_SUFFIXES:
            flag_file = sid.flag_path(suffix)
            if flag_file.exists():
                flag_file.unlink()


if __name__ == "__main__":
    main()
