#!/usr/bin/env python3
"""
Shared git helpers for the runner (#253).

Three runner modules (executor.py, auto_harden.py, approval.py) each
used to define an identical `git()` wrapper over `subprocess.run`.
Executor also defined `_format_git_error`, which the others lacked
and therefore reimplemented inconsistently. Consolidating both here
removes the drift risk — future fixes land in one place.
"""
import subprocess


def git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command and return the CompletedProcess.

    Captures both stdout and stderr as text. Never raises on non-zero
    exit — callers inspect `returncode` and format errors via
    `format_git_error()` below.
    """
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True,
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
