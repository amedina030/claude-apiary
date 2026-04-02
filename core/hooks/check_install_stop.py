#!/usr/bin/env python3
"""
Stop hook — cleans up session flag files written by core PreToolUse hooks
(check_install.py, inject_session.py).
"""
import sys
import json
from pathlib import Path

SESSION_FLAG_DIR = Path.home() / ".claude" / "tmp"

FLAG_SUFFIXES = ["_install_checked", "_session_injected"]


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    if session_id:
        for suffix in FLAG_SUFFIXES:
            flag_file = SESSION_FLAG_DIR / f"{session_id}{suffix}"
            if flag_file.exists():
                flag_file.unlink()


if __name__ == "__main__":
    main()
