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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.hook_context import run_standalone
from core.utils.filelock import FileLock
from core.session import dump_history, load_history, sessions_dir, sweep_stale_session_files
from core.utils.atomic import write_text_atomic
from core.utils.timeutil import now_iso

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
            "ended_at": now_iso(),
            "role": role,
            "mission": mission,
            "registered": registered,
        })

        # Cap at MAX_HISTORY
        history = history[-MAX_HISTORY:]

        write_text_atomic(hist, dump_history(history))


def run(payload: dict):
    """Record this session in the history ring buffer. Never adds context.

    Failures are the caller's problem to log (dispatcher → hooks.log, or the
    standalone shim → stderr): a FileLock timeout or an OSError used to
    surface as a hook error at the end of every turn (review Bug 11).
    """
    # Runner stage subprocesses are not real user sessions — never log them
    # to the history or last-session records (#223).
    if os.environ.get(RUNNER_SUBPROCESS_ENV_VAR) == "1":
        return None

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    if not session_id or not transcript_path:
        return None

    # Append to session history ring buffer
    _append_to_history(session_id, transcript_path)

    # Write current session metadata
    last = last_session_path()
    last.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        last, json.dumps({"session_id": session_id, "transcript_path": transcript_path}),
    )
    # Bound the per-session file growth (review S1, second half).
    sweep_stale_session_files()
    return None


if __name__ == "__main__":
    run_standalone(run, event="Stop")
