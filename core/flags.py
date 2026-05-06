"""
Flag file management for claude-apiary tools.

Each tool feature is toggled via a sentinel file. After the per-repo
migration (MIGRATION-PLAN.md §3.5 D21), the canonical location is
``<repo>/.claude/apiary/flags/<flag_name>-enabled``. The historical
``~/.claude/<flag_name>-enabled`` location is read as a fallback during
phases 1–4 of the migration so existing global toggles keep working
until each repo is reinstalled. Phase 5 cleanup removes the fallback.

Repo discovery for the per-repo path:

1. ``$CLAUDE_PROJECT_DIR`` (set by Claude Code at hook-fire time) — the
   most reliable signal in the hook context.
2. ``$APIARY_TARGET_REPO`` — explicit override for tests / CLI.
3. The git root containing the cwd, when neither env var is set.

If none of these resolve to a directory with a ``.claude/apiary/`` dir,
``is_enabled`` falls back to the global location only.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PIN_FLAGS_SUBPATH = ".claude/apiary/flags"


def _per_repo_root() -> Path | None:
    """Best-effort resolution of the bootstrapped repo we should be reading
    per-repo flags from. Returns None when no repo can be located, in
    which case callers fall back to the global flag path."""
    for env in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).is_dir():
            return Path(val)
    # Last-ditch: cwd's git root. Cheap enough for one git invocation per
    # process, and only matters outside hooks (CLI tools).
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


def _per_repo_flag_path(repo: Path, flag_name: str) -> Path:
    return repo / PIN_FLAGS_SUBPATH / f"{flag_name}-enabled"


def _global_flag_path(flag_name: str) -> Path:
    return CLAUDE_DIR / f"{flag_name}-enabled"


def is_enabled(flag_name: str) -> bool:
    """Return True iff the flag is enabled in the current repo (or globally
    as a phase-1-through-4 fallback).

    Per-repo (``<repo>/.claude/apiary/flags/<flag_name>-enabled``) wins
    over global. The fallback is removed in phase 5 of the per-repo
    migration — see MIGRATION-PLAN.md §10 phase 5."""
    repo = _per_repo_root()
    if repo is not None:
        per_repo = _per_repo_flag_path(repo, flag_name)
        if per_repo.exists():
            return True
        # Per-repo dir exists but flag doesn't → repo is bootstrapped and
        # flag is explicitly off. We do NOT consult the global fallback in
        # this case so each repo's "off" state is honored.
        if per_repo.parent.parent.is_dir():  # .claude/apiary/ exists
            return False
    # No bootstrapped repo in scope → consult the global path (legacy).
    return _global_flag_path(flag_name).exists()


def enable(flag_name: str) -> None:
    """Enable the flag for the current repo (preferred) or globally (fallback).

    Writes to the per-repo location when a bootstrapped repo can be
    resolved; otherwise writes to ``~/.claude/<flag>-enabled`` for
    backward compat. Phase-5 cleanup will drop the global branch."""
    repo = _per_repo_root()
    if repo is not None and (repo / ".claude" / "apiary").is_dir():
        target = _per_repo_flag_path(repo, flag_name)
    else:
        target = _global_flag_path(flag_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("enabled", encoding="utf-8")


def disable(flag_name: str) -> None:
    """Delete the flag in the current repo (preferred) or globally (fallback).

    Mirror of ``enable``. Removes ONLY the per-repo file when a
    bootstrapped repo is in scope, leaving the global file alone — that
    preserves the phase-1-through-4 invariant where re-bootstrapping
    propagates the global state forward exactly once."""
    repo = _per_repo_root()
    if repo is not None and (repo / ".claude" / "apiary").is_dir():
        target = _per_repo_flag_path(repo, flag_name)
    else:
        target = _global_flag_path(flag_name)
    if target.exists():
        target.unlink()


def toggle(flag_name: str) -> bool:
    """Toggle flag. Returns new state (True = enabled)."""
    if is_enabled(flag_name):
        disable(flag_name)
        return False
    enable(flag_name)
    return True
