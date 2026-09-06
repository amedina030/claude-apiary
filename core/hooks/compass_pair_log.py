#!/usr/bin/env python3
"""
Stop hook — logs (assistant_text, user_turn) pairs for the compass rule table.

At the end of every assistant turn, appends the pairs the session transcript
gained since the previous Stop to ``<state-dir>/compass/turns/<sid>.jsonl``
(D-2026-62 step 1, T-2026-319). The classifier (``compass/classify.py``) reads
that file at ``/wrapup`` or in the nightly catch-up; nothing here calls a model
and nothing here adds context to the session.

Why a Stop hook and not ``/wrapup`` alone: Claude Code prunes transcripts
(L-2026-180), so the pairs must be captured while the session is alive. The
byte-offset cursor beside the turns file keeps each call proportional to the
turn that just finished (see ``compass/turns.py``).

Skipped entirely when ``APIARY_RUNNER_SUBPROCESS=1`` — runner stage subprocesses
have no user turns to pair (their records are ``entrypoint == "sdk-cli"``, which
the record filter drops anyway; the env check saves opening the file).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.hook_context import run_standalone

# Mirror of runner/claude_subprocess.py::RUNNER_SUBPROCESS_ENV_VAR — hardcoded
# rather than imported to avoid a core -> runner dependency edge.
RUNNER_SUBPROCESS_ENV_VAR = "APIARY_RUNNER_SUBPROCESS"


def run(payload: dict):
    """Append this turn's pairs to the session's turns file. Never adds context.

    Failures propagate to the dispatcher, which logs them to ``hooks.log`` and
    carries on; under the standalone shim they degrade to a no-objection reply.
    """
    if os.environ.get(RUNNER_SUBPROCESS_ENV_VAR) == "1":
        return None

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    if not session_id or not transcript_path:
        return None

    from compass import turns

    turns.update_from_transcript(transcript_path, session_id)
    return None


if __name__ == "__main__":
    run_standalone(run, event="Stop")
