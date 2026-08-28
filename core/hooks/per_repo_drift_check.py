#!/usr/bin/env python3
"""PreToolUse hook — per-repo drift detection on first tool call of a session.

Wraps :func:`core.drift.check_and_handle` so a buggy drift handler can never
block tool calls: ``run`` swallows nothing itself, but the dispatcher (and the
standalone shim) run it fail-open and log the failure. Per the hook standard
(``docs/standards/code-style.md``), hooks must always exit 0.

Drift findings (move/copy/skip) print a one-line message to stderr so the user
sees what happened, but the hook still exits 0 — Claude Code shows stderr
inline and proceeds with the tool call.

Once per session (core review Bug 9). The docstring and
``docs/architecture/per-repo-install.md`` have always said "on the first tool
call of a session", but until 2026-08 there was no guard, so ``check_and_handle``
rewrote ``self-pointer.json`` on *every* tool call — a needless write plus a
Windows ``tmp.replace`` race against the other hooks reading the same file. The
guard is the same ``SessionId.flag_path`` mechanism ``inject_session`` uses. A
payload with no usable session id runs the check (correctness over frequency).

Runs first in the PreToolUse dispatcher chain so the rest of the chain sees an
up-to-date self-pointer and registry entry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.hook_context import HookResult, run_standalone

# Suffix for the once-per-session flag file (SessionId.flag_path).
CHECKED_FLAG = "drift_checked"


def _repo_root() -> Path:
    """Repo root from the env var Claude Code sets at hook-fire time.
    Falls back to cwd when running outside a hook (e.g. in tests)."""
    env = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def _already_checked(payload: dict) -> bool:
    """True when this session already ran the drift check (and mark it run).

    Unresolvable session id → False, so the check still runs.
    """
    from core.session import SessionId

    raw_id = payload.get("session_id", "") or ""
    if not raw_id:
        return False
    try:
        sid = SessionId(raw_id)
    except ValueError:
        return False
    flag_file = sid.flag_path(CHECKED_FLAG)
    if flag_file.exists():
        return True
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    flag_file.write_text("1", encoding="utf-8")
    return False


def run(payload: dict) -> HookResult | None:
    """Reconcile the registry when the repo moved/was copied. Never blocks."""
    if _already_checked(payload):
        return None

    from core import drift

    report = drift.check_and_handle(_repo_root())
    if report.action in ("skip", "move", "copy"):
        # Drift outcomes worth surfacing to the user. stderr, not context:
        # this is an operator message, not something Claude should reason about.
        sys.stderr.write(f"[apiary {report.action.upper()}] {report.message}\n")
    # action == "none" or "not_bootstrapped" → silent in the no-action case.
    return None


if __name__ == "__main__":
    run_standalone(run)
