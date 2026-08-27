#!/usr/bin/env python3
"""PreToolUse hook — block `git push` when the doc-conformer fails.

This is the *enforcement* half of the doc-conformance loop. The checker
``docs/check_cli_claims.py`` (shipped T-2026-211) verifies that the CLI
subcommands/flags documented in ``docs/reference/cli-tools.md`` still match
each tool's real argparse. It is fast and report-only, but nothing ran it
automatically — so drift could still rot in silently between manual runs.
A written "remember to run it before pushing" instruction has the same
failure mode as the rot it guards against (relies on remembering), so the
gate lives here instead: deterministic, can't-forget, fired at the exact
moment a push is attempted.

Behaviour:
  - Fires on Bash tool calls whose command performs a ``git push``.
  - Resolves the git root of the repo being pushed (from the payload cwd),
    and only acts if that repo actually has ``docs/check_cli_claims.py`` —
    so it's a no-op in target repos that don't ship the conformer (the hook
    is installed toolkit-wide; the guard keeps it inert where irrelevant).
  - Runs the conformer. Exit 0 → allow the push. Nonzero → BLOCK with the
    conformer's output as the reason, so the drift is surfaced and fixed
    before anything leaves the working tree.

Fail-open on *internal* error (bad payload, conformer not runnable, timeout):
a buggy gate must never wedge your ability to push. The only thing that
blocks is a clean, intentional nonzero exit from the conformer itself —
i.e. real, detected drift.
"""

from __future__ import annotations

import re

# Shell separators that delimit independent commands inside one Bash string.
# A push hidden behind `&&`/`;`/`|` still counts, so we scan each segment.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|\|")

# git *global* options that consume the following token as their value. They
# can sit between ``git`` and the subcommand (``git -C /repo push``), so the
# value token must be skipped when scanning for the subcommand.
_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)


def _segment_pushes(segment: str) -> bool:
    """True iff a single shell segment invokes ``git push``."""
    tokens = segment.split()
    i = 0
    # Skip leading ``KEY=val`` env assignments (e.g. ``GIT_SSH=x git push``).
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
        key = tokens[i].split("=", 1)[0]
        if key and all(c.isalnum() or c == "_" for c in key):
            i += 1
        else:
            break
    if i >= len(tokens):
        return False

    # The command word must be git (bare, or a path ending in /git or \git).
    base = tokens[i].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if base not in ("git", "git.exe"):
        return False
    i += 1

    # The first positional token after git (skipping global options) is the
    # subcommand. Only ``push`` counts.
    while i < len(tokens):
        tok = tokens[i]
        if tok in _VALUE_FLAGS:
            i += 2  # skip the flag and the value it consumes
            continue
        if tok.startswith("-"):
            i += 1  # no-value flag or inline ``--opt=val``
            continue
        return tok == "push"
    return False


def command_pushes(command: str) -> bool:
    """True iff a Bash command string performs a ``git push`` in any segment.

    Pure + fully unit-testable. Conservative: it only fires on a real
    ``git push`` subcommand, so it won't block an unrelated command that
    merely mentions the word ``push`` (e.g. ``echo "git push"``).
    """
    if not command or "push" not in command:
        return False
    return any(_segment_pushes(seg) for seg in _SEGMENT_SPLIT.split(command))


def run(payload: dict):  # pragma: no cover — pure logic lives in
    #                       command_pushes (tested); this is the subprocess shell.
    """Block a ``git push`` when the pushed repo's doc-conformer reports drift."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.hook_context import HookResult
    from core.utils.gitutil import git_root

    # Runner subprocesses are one-shot workers; the push gate is out of scope
    # for them (consistent with the other core hooks, #228).
    if os.environ.get("APIARY_RUNNER_SUBPROCESS") == "1":
        return None

    if (payload.get("tool_name") or "") != "Bash":
        return None

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not command or not command_pushes(command):
        return None

    # Resolve the git root of the repo being pushed (from the session cwd).
    cwd = payload.get("cwd") or os.getcwd()
    root = git_root(cwd)
    if root is None:
        return None

    # No-op unless the repo being pushed actually ships the conformer.
    conformer = root / "docs" / "check_cli_claims.py"
    if not conformer.is_file():
        return None

    try:
        result = subprocess.run(
            [sys.executable, str(conformer)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Conformer not runnable → fail open; don't wedge the push on a
        # broken gate. Real drift (a clean nonzero exit) is handled below.
        return None

    if result.returncode == 0:
        return None

    detail = (result.stdout or "").strip() or (result.stderr or "").strip()
    return HookResult(
        block_reason=(
            "Push blocked: doc-conformer (docs/check_cli_claims.py) found drift "
            "between docs/reference/cli-tools.md and the tools' real argparse. "
            "Fix the drift before pushing — usually the doc is the richer source, "
            "so prefer correcting the doc unless the code genuinely changed.\n\n"
            f"{detail}"
        )
    )


if __name__ == "__main__":  # pragma: no cover
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from core.hook_context import run_standalone

    run_standalone(run)
