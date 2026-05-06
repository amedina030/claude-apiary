"""
Flag file management for claude-apiary tools.

Each toggle is a sentinel file at
``<repo>/.claude/apiary/flags/<flag_name>-enabled``. Apiary tools call
``is_enabled(name)``; ``/budgeter-log`` and the other slash-command
toggles call ``enable``/``disable``/``toggle``. Repo discovery order:

1. ``$CLAUDE_PROJECT_DIR`` (set by Claude Code at hook-fire time).
2. ``$APIARY_TARGET_REPO`` — explicit override for tests / CLI.
3. The git root containing cwd, when neither env var is set.

When none of those resolve to a directory, the helpers raise
``RuntimeError`` — there's no global fallback post-migration
(MIGRATION-PLAN.md §10 phase 5).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

PIN_FLAGS_SUBPATH = ".claude/apiary/flags"


class FlagsRepoUnresolved(RuntimeError):
    """Raised when no bootstrapped repo is in scope for a flag operation."""


def _per_repo_root() -> Path | None:
    """Best-effort resolution of the bootstrapped repo. Returns None when
    no repo can be located — caller decides how to react."""
    for env in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).is_dir():
            return Path(val)
    # Last-ditch: cwd's git root. Cheap enough for one git invocation per
    # process; only matters outside hooks (CLI tools).
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return None


def _flag_path(flag_name: str) -> Path:
    """Return the absolute path of the flag file in the current repo.

    Raises ``FlagsRepoUnresolved`` when no bootstrapped repo can be
    located, so callers don't silently miss a misconfigured environment.
    """
    repo = _per_repo_root()
    if repo is None:
        raise FlagsRepoUnresolved(
            f"cannot resolve a bootstrapped repo for flag {flag_name!r}; "
            "set CLAUDE_PROJECT_DIR or APIARY_TARGET_REPO, or run from "
            "inside a bootstrapped repo's git tree."
        )
    return repo / PIN_FLAGS_SUBPATH / f"{flag_name}-enabled"


def is_enabled(flag_name: str) -> bool:
    """Return True iff ``<repo>/.claude/apiary/flags/<flag>-enabled`` exists."""
    try:
        return _flag_path(flag_name).is_file()
    except FlagsRepoUnresolved:
        # Hooks call this without a clear repo in some edge cases (e.g.
        # incubator spawning a fresh repo). Treat unresolvable as
        # "disabled" rather than crashing the caller.
        return False


def enable(flag_name: str) -> None:
    """Create the flag file for the current repo."""
    target = _flag_path(flag_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("enabled", encoding="utf-8")


def disable(flag_name: str) -> None:
    """Remove the flag file for the current repo if present."""
    try:
        target = _flag_path(flag_name)
    except FlagsRepoUnresolved:
        return
    if target.exists():
        target.unlink()


def toggle(flag_name: str) -> bool:
    """Toggle flag. Returns new state (True = enabled)."""
    if is_enabled(flag_name):
        disable(flag_name)
        return False
    enable(flag_name)
    return True
