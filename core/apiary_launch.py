#!/usr/bin/env python3
"""Apiary hook launcher -- locates the apiary repo via ~/.claude/apiary.json
and runs the specified hook script, regardless of which repo the Claude Code
session was started in.

Installed to ``~/.claude/apiary_launch.py`` by ``setup.py --global``.
Source of truth: ``core/apiary_launch.py`` in the apiary repo.

Usage (in settings.json hook commands)::

    python ~/.claude/apiary_launch.py core/hooks/startup_prompt_hook.py

The launcher resolves the apiary repo root from the pointer file, constructs
the full script path, and execs it as a subprocess so that ``__file__`` in the
hook script resolves correctly.  Falls back to ``$CLAUDE_PROJECT_DIR`` when
the pointer file is missing (session is inside the apiary repo itself).
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: apiary_launch.py <relative-hook-path>", file=sys.stderr)
        return 1

    hook_rel = sys.argv[1]
    repo_path = None

    # 1. Try the apiary pointer file (cross-repo case).
    pointer = Path.home() / ".claude" / "apiary.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
            candidate = data.get("repo_path", "")
            if candidate and Path(candidate).is_dir():
                repo_path = Path(candidate)
        except (json.JSONDecodeError, OSError):
            pass

    # 2. Fall back to CLAUDE_PROJECT_DIR (works when session IS in apiary).
    if repo_path is None:
        cpd = os.environ.get("CLAUDE_PROJECT_DIR", "")
        if cpd:
            repo_path = Path(cpd)

    if repo_path is None:
        print("error: cannot locate apiary repo (no pointer file, no CLAUDE_PROJECT_DIR)", file=sys.stderr)
        return 1

    script = repo_path / hook_rel
    if not script.is_file():
        # Silent exit -- avoids noisy failures when apiary repo moved.
        return 0

    result = subprocess.run(
        [sys.executable, str(script)],
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo_path)},
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
