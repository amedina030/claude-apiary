#!/usr/bin/env python3
"""Stop hook — cleans up the notes loaded flag file."""
import sys
import json
from pathlib import Path

SESSION_FLAG_DIR = Path.home() / ".claude" / "tmp"


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    if session_id:
        flag_file = SESSION_FLAG_DIR / f"{session_id}_notes_loaded"
        if flag_file.exists():
            flag_file.unlink()


if __name__ == "__main__":
    main()
