#!/usr/bin/env python3
"""
Stop hook — preserves session metadata and history for handoff generation.

Tracks current session in .last-session.json and maintains a ring buffer
of recent sessions in .session-history.json with identity tags (role/mission).

Skipped entirely when ``APIARY_RUNNER_SUBPROCESS=1`` is set in the env —
runner stage subprocesses are not real user sessions and must not appear
in the history ring buffer (#223). The constant name matches the canonical
``RUNNER_SUBPROCESS_ENV_VAR`` defined in ``runner/claude_subprocess.py``.
"""
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.utils.filelock import FileLock

CLAUDE_DIR = Path.home() / ".claude"
SESSION_PATH = CLAUDE_DIR / ".last-session.json"
HISTORY_PATH = CLAUDE_DIR / ".session-history.json"
MAX_HISTORY = 10

# Mirror of runner/claude_subprocess.py::RUNNER_SUBPROCESS_ENV_VAR. Hardcoded
# rather than imported to avoid a core → runner dependency edge.
RUNNER_SUBPROCESS_ENV_VAR = "APIARY_RUNNER_SUBPROCESS"


def _append_to_history(session_id, transcript_path):
    """Append session to history ring buffer with identity tags."""
    from core.session import load_identity
    identity = load_identity(session_id)
    role, mission, registered = identity["role"], identity["mission"], identity["registered"]

    with FileLock(HISTORY_PATH):
        if HISTORY_PATH.exists():
            try:
                history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                history = []
        else:
            history = []

        # Dedup: remove existing entry for this session_id
        history = [h for h in history if h.get("session_id") != session_id]

        history.append({
            "session_id": session_id,
            "transcript_path": transcript_path,
            "ended_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "role": role,
            "mission": mission,
            "registered": registered,
        })

        # Cap at MAX_HISTORY
        history = history[-MAX_HISTORY:]

        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")


def main():
    # Runner stage subprocesses are not real user sessions — never log them
    # to .session-history.json or .last-session.json. They would otherwise
    # appear permanently in the unseen-sessions list every time a fresh
    # session starts (#223).
    if os.environ.get(RUNNER_SUBPROCESS_ENV_VAR) == "1":
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    if not session_id or not transcript_path:
        sys.exit(0)

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)

    # Append to session history ring buffer
    _append_to_history(session_id, transcript_path)

    # Write current session metadata
    SESSION_PATH.write_text(
        json.dumps({"session_id": session_id, "transcript_path": transcript_path}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
