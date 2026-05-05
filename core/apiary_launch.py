#!/usr/bin/env python3
"""Apiary launcher -- locates the apiary repo via ~/.claude/apiary.json
and runs the specified script with forwarded arguments, regardless of
which repo the Claude Code session was started in.

Installed to ``~/.claude/apiary_launch.py`` by ``setup.py --global``.
Source of truth: ``core/apiary_launch.py`` in the apiary repo.

Usage (hooks in settings.json)::

    python ~/.claude/apiary_launch.py core/hooks/startup_prompt_hook.py

Usage (CLI tools from skill templates)::

    python ~/.claude/apiary_launch.py scribe/notes.py list --type todo
    python ~/.claude/apiary_launch.py budgeter/report.py --since 7d

The launcher resolves the apiary repo root from the pointer file and runs
the script as a subprocess with all remaining arguments forwarded.

The subprocess inherits the *caller's* cwd unchanged -- the launcher does
NOT chdir into the apiary repo, because doing so makes tools like scribe
(which use ``git rev-parse --show-toplevel`` to find the active repo)
resolve to apiary instead of the session's actual repo, so notes/state
land in the wrong place.  Apiary scripts find their own code via
``Path(__file__)``, not cwd, so they don't need the chdir.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Env var the launcher sets so child scripts (scribe/runner/captures/...)
# can find their per-target state dir without re-running the registry
# lookup. Mirrors core.utils.state.TARGET_STATE_DIR_ENV — kept as a
# literal here so this file stays importable without core/utils on path.
_TARGET_STATE_DIR_ENV = "APIARY_TARGET_STATE_DIR"
_LEGACY_LAYOUT_ENV = "APIARY_STATE_LAYOUT"


def _resolve_target_state_dir(repo_path: Path) -> "Path | None":
    """Resolve the per-target state dir for the launcher's caller cwd, or
    None if it can't be determined. Never raises — the launcher must not
    block hooks on resolver errors."""
    # Legacy escape hatch — skip the resolver entirely.
    if os.environ.get(_LEGACY_LAYOUT_ENV, "").strip().lower() == "legacy":
        return None
    try:
        sys.path.insert(0, str(repo_path))
        from core.utils import state as _state
        return _state.resolve_target_state_dir(apiary_repo=repo_path)
    except Exception:
        return None
    finally:
        try:
            sys.path.remove(str(repo_path))
        except ValueError:
            pass


def _resolve_repo_path() -> "Path | None":
    """Locate the apiary repo root from the pointer file or env var."""
    # 1. Try the apiary pointer file (cross-repo case).
    pointer = Path.home() / ".claude" / "apiary.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
            candidate = data.get("repo_path", "")
            if candidate and Path(candidate).is_dir():
                return Path(candidate)
        except (json.JSONDecodeError, OSError):
            pass
    # 2. Fall back to CLAUDE_PROJECT_DIR (works when session IS in apiary).
    cpd = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if cpd:
        return Path(cpd)
    return None


def main() -> int:
    # Meta-command: print the repo path and exit.
    if len(sys.argv) == 2 and sys.argv[1] == "--print-repo-path":
        repo_path = _resolve_repo_path()
        if repo_path is None:
            print("error: cannot locate apiary repo", file=sys.stderr)
            return 1
        print(repo_path)
        return 0

    if len(sys.argv) < 2:
        print("usage: apiary_launch.py <relative-hook-path>", file=sys.stderr)
        return 1

    hook_rel = sys.argv[1]
    repo_path = _resolve_repo_path()

    if repo_path is None:
        print("error: cannot locate apiary repo (no pointer file, no CLAUDE_PROJECT_DIR)", file=sys.stderr)
        return 1

    script = repo_path / hook_rel
    if not script.is_file():
        # Silent exit -- avoids noisy failures when apiary repo moved.
        return 0

    # Inherit cwd and env from caller -- don't chdir into apiary or override
    # CLAUDE_PROJECT_DIR.  See module docstring for why.
    env = os.environ.copy()
    if not env.get(_TARGET_STATE_DIR_ENV, "").strip():
        # Lazy auto-registration: first state-touching call from a new
        # path creates a .repos/<name>-<id>/ entry. If the caller is not
        # in a git repo, or anything else fails, the env var is left
        # unset and child tools fall back to their existing logic.
        state_dir = _resolve_target_state_dir(repo_path)
        if state_dir is not None:
            env[_TARGET_STATE_DIR_ENV] = str(state_dir)
    result = subprocess.run([sys.executable, str(script)] + sys.argv[2:], env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
