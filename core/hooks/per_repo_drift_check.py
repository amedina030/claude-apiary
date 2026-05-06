#!/usr/bin/env python3
"""PreToolUse hook — per-repo drift detection on first tool call of a session.

Wraps :func:`core.drift.check_and_handle` with a try/except so a buggy
drift handler can never block tool calls. Per the hook standard
(``docs/standards/code-style.md``), hooks must always exit 0.

Drift findings (move/copy/skip) print a one-line message to stderr so
the user sees what happened, but the hook still exits 0 — Claude Code
shows stderr inline and proceeds with the tool call.

Hook entry installed by ``apiary install --target <repo>`` looks like::

    "command": "python \"$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py\" core/hooks/per_repo_drift_check.py"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Resolve repo root from the env var set by Claude Code at hook-fire time.
# Fall back to cwd when running outside a Claude Code hook (e.g. in tests).
_REPO_ROOT_ENV = os.environ.get("CLAUDE_PROJECT_DIR", "")


def _repo_root() -> Path:
    if _REPO_ROOT_ENV:
        return Path(_REPO_ROOT_ENV).resolve()
    return Path.cwd().resolve()


def main() -> int:
    try:
        # Lazy import — keeps hook startup snappy when there's nothing
        # to do (the import sets up sys.path under the launcher).
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from core import drift

        repo_root = _repo_root()
        report = drift.check_and_handle(repo_root)
        if report.action in ("skip", "move", "copy"):
            # Drift outcomes worth surfacing to the user.
            tag = report.action.upper()
            sys.stderr.write(f"[apiary {tag}] {report.message}\n")
        # action == "none" or "not_bootstrapped" → silent in the no-action case.
    except Exception:
        # Hooks must not crash. Swallow everything — `apiary doctor` can
        # surface persistent failures via its own checks.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
