#!/usr/bin/env python3
"""Shared git helpers for the runner (#253).

Created to end the drift between the identical ``git()`` wrappers that
executor.py, auto_harden.py and approval.py each defined — but only ``git()``
and ``format_git_error()`` ever moved, so ``branch_exists`` / ``checkout`` /
``current_branch`` stayed duplicated 2-3 times each and promptly drifted:
``auto_harden.branch_exists`` never got the ``refs/heads/`` fix that
``executor.branch_exists`` got for ATK-006, so a remote tracking ref could
satisfy it. The consolidation is finished here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# The branch a run owns, published by the orchestrator into the stage
# subprocess environment. One branch per run: stage 4 works on the branch the
# worktree is already on instead of creating a second `runner/<uuid>` beside
# it (review runner Bug 3), and stages 5/6 and queue.py all name the same one.
RUNNER_BRANCH_ENV = "APIARY_RUNNER_BRANCH"


def git(*args: str, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the CompletedProcess.

    Captures stdout and stderr as UTF-8 text — explicitly, because git echoes
    back LLM-authored commit subjects, and decoding those with the Windows
    ANSI codepage raises UnicodeDecodeError on the first non-ASCII character
    (review runner Bug 7). Never raises on non-zero exit — callers inspect
    ``returncode`` and format errors via ``format_git_error()``.

    ``cwd`` defaults to the process's working directory, which the
    orchestrator has already pointed at the worktree or the target repo.
    """
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd) if cwd is not None else None,
    )


def format_git_error(action: str, result: subprocess.CompletedProcess, extra: str = "") -> str:
    """Build a RuntimeError message that includes both stdout and stderr.

    Git often writes failure context (e.g. 'nothing to commit') to stdout, not
    stderr, so surfacing only stderr leaves operators with empty error messages.
    """
    parts = [f"Git error {action} (exit {result.returncode})"]
    if extra:
        parts.append(extra)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        parts.append(f"stdout: {stdout}")
    if stderr:
        parts.append(f"stderr: {stderr}")
    if not stdout and not stderr:
        parts.append("(no output captured)")
    return "\n".join(parts)


def branch_exists(branch: str, *, cwd: Path | str | None = None) -> bool:
    """True if a *local* branch of that name exists.

    ``refs/heads/`` is explicit (ATK-006): a bare ``rev-parse --verify <name>``
    also matches a remote tracking ref, so a deleted local branch with a
    surviving ``origin/<name>`` looked like it still existed.
    """
    return git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=cwd).returncode == 0


def current_branch(*, cwd: Path | str | None = None) -> str:
    """Short name of the checked-out branch (empty on error / detached HEAD)."""
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()


def checkout(branch: str, *, cwd: Path | str | None = None) -> None:
    """Check out an existing branch. Raises RuntimeError on failure."""
    result = git("checkout", branch, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(format_git_error(f"checking out branch '{branch}'", result))


def create_branch(branch: str, *, cwd: Path | str | None = None) -> None:
    """Create and check out a new branch. Raises RuntimeError on failure."""
    result = git("checkout", "-b", branch, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(format_git_error(f"creating branch '{branch}'", result))


def run_branch_from_env(uuid: str) -> str:
    """The branch this run owns.

    The orchestrator publishes it in ``APIARY_RUNNER_BRANCH``; a stage invoked
    standalone (``python -m runner.executor <plan>``) falls back to the
    historical ``runner/<uuid>``.
    """
    named = (os.environ.get(RUNNER_BRANCH_ENV) or "").strip()
    return named or f"runner/{uuid}"
