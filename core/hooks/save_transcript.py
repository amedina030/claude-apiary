#!/usr/bin/env python3
"""
Stop hook — preserves session metadata and history for handoff generation.

Tracks the current session in ``<state-dir>/sessions/last-session.json``
and maintains a ring buffer of recent sessions in
``<state-dir>/sessions/history.json`` with identity tags (role/mission).
Nothing is written under ``~/.claude`` (review S1).

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
from core.session import dump_history, load_history, sessions_dir

MAX_HISTORY = 10


def history_path() -> Path:
    return sessions_dir() / "history.json"


def last_session_path() -> Path:
    return sessions_dir() / "last-session.json"

# Mirror of runner/claude_subprocess.py::RUNNER_SUBPROCESS_ENV_VAR. Hardcoded
# rather than imported to avoid a core → runner dependency edge.
RUNNER_SUBPROCESS_ENV_VAR = "APIARY_RUNNER_SUBPROCESS"


def _append_to_history(session_id, transcript_path):
    """Append session to history ring buffer with identity tags."""
    from core.session import load_identity
    identity = load_identity(session_id)
    role, mission, registered = identity["role"], identity["mission"], identity["registered"]

    hist = history_path()
    hist.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(hist):
        history = load_history(hist)

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

        hist.write_text(dump_history(history), encoding="utf-8")


def main():
    """Stop hook: never crash (a FileLock timeout or an OSError used to
    surface as a hook error at the end of every turn — review Bug 11)."""
    try:
        _run()
    except Exception as exc:  # noqa: BLE001 — hooks must not crash
        print(f"[save_transcript] failed: {exc!r}", file=sys.stderr)
    sys.exit(0)


def _run():
    # Runner stage subprocesses are not real user sessions — never log them
    # to the history or last-session records (#223).
    if os.environ.get(RUNNER_SUBPROCESS_ENV_VAR) == "1":
        return

    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    if not session_id or not transcript_path:
        return

    # Append to session history ring buffer
    _append_to_history(session_id, transcript_path)

    # Write current session metadata
    last = last_session_path()
    last.parent.mkdir(parents=True, exist_ok=True)
    last.write_text(
        json.dumps({"session_id": session_id, "transcript_path": transcript_path}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
