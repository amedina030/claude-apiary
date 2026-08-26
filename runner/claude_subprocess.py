"""Shared `claude -p` subprocess runner for runner stages.

Centralizes the spawn so every runner subprocess:
  1. Sets ``APIARY_RUNNER_SUBPROCESS=1`` in its env, telling startup hooks
     in the spawned Claude Code session to skip context injection (identity,
     active notes summary, full learnings dump, CLI tools index). These
     payloads are useful to interactive sessions but pure overhead for a
     one-shot runner worker — typically tens of KB of input tokens that
     the worker doesn't read.
  2. Emits a ``<usage>`` XML block via cost_emit so run.py can attribute
     per-stage tokens.
  3. Forwards only an allowlisted subset of the parent environment, so
     unrelated secrets in the parent process (AWS keys, cloud tokens,
     SSH agent sockets, etc.) are not exposed to the spawned claude CLI
     or the tools it invokes. Claude-, proxy-, and apiary-specific vars
     are pass-through; system essentials for each platform are named
     explicitly. Set ``APIARY_RUNNER_ALLOW_ALL_ENV=1`` in the parent env
     as a temporary escape hatch if the allowlist breaks a legitimate
     need.

The startup hooks (core/hooks/startup_prompt_hook.py and
core/hooks/startup_hook.py) check the env var on entry and short-circuit
to a no-context allow when it is set.

Module-level constants
----------------------
RUNNER_SUBPROCESS_ENV_VAR : str
    Name of the environment variable set in every spawned subprocess.
    Import this constant from here rather than re-defining it in hooks so
    both sides stay in sync automatically.
ALLOW_ALL_ENV_VAR : str
    Name of the escape-hatch env var. If set to "1" in the parent env,
    the full parent environment is forwarded (legacy behavior).
"""
import os
import subprocess

from .cost_emit import emit_usage_xml

RUNNER_SUBPROCESS_ENV_VAR = "APIARY_RUNNER_SUBPROCESS"
ALLOW_ALL_ENV_VAR = "APIARY_RUNNER_ALLOW_ALL_ENV"

# System-essential vars, named explicitly per platform. Missing any of
# these from a claude subprocess would typically break basic operation
# (PATH lookup, HOME-relative config, locale, temp dirs).
_POSIX_SYSTEM_VARS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
})
_WINDOWS_SYSTEM_VARS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "TEMP", "TMP",
    "USERNAME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
})

# HTTP(S) proxy vars — claude CLI's network stack reads these. Listed
# in both cases because Windows env var names are case-insensitive but
# subprocess inherits them with their original casing.
_PROXY_VARS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
})

# Any var starting with one of these prefixes is forwarded. Covers
# claude CLI config (ANTHROPIC_API_KEY, CLAUDE_*), apiary's own runtime
# flags (APIARY_*), and the runner-subprocess sentinel itself.
_ALLOWED_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "APIARY_")


def _build_subprocess_env(parent_env: dict[str, str] | None = None,
                          *, is_windows: bool | None = None) -> dict[str, str]:
    """Build the env dict for a runner-spawned claude subprocess.

    Forwards only allowlisted vars from ``parent_env`` (defaults to
    ``os.environ``). Always sets ``RUNNER_SUBPROCESS_ENV_VAR=1``. If
    ``ALLOW_ALL_ENV_VAR`` is "1" in the parent env, returns the full
    parent env as an escape hatch.

    ``is_windows`` picks the platform allowlist; defaults to detecting
    ``os.name == 'nt'``. Exposed as a parameter for test coverage.
    """
    if parent_env is None:
        parent_env = os.environ  # type: ignore[assignment]
    if is_windows is None:
        is_windows = os.name == "nt"

    if parent_env.get(ALLOW_ALL_ENV_VAR) == "1":
        return {**parent_env, RUNNER_SUBPROCESS_ENV_VAR: "1"}

    system_vars = _WINDOWS_SYSTEM_VARS if is_windows else _POSIX_SYSTEM_VARS
    env: dict[str, str] = {}
    for name, value in parent_env.items():
        if name in system_vars or name in _PROXY_VARS:
            env[name] = value
            continue
        if any(name.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
            env[name] = value
    env[RUNNER_SUBPROCESS_ENV_VAR] = "1"
    return env

# Maximum bytes buffered from stdout/stderr before we truncate.  Prevents
# unbounded memory growth for unexpectedly large subprocess outputs.
_MAX_OUTPUT_BYTES = 50 * 1024 * 1024  # 50 MB

# Every runner-spawned claude gets these regardless of what the operator's
# own Claude Code settings allow (review runner: the subprocess inherited
# `Bash(git push *)` from .claude/settings.local.json). Both rule spellings
# are passed because Claude Code accepted `Bash(cmd:*)` before `Bash(cmd *)`.
DEFAULT_DISALLOWED_TOOLS = ("Bash(git push *)", "Bash(git push:*)")
# Hard ceiling on agentic turns per stage call so a looping subprocess
# cannot run until the timeout alone stops it.
DEFAULT_MAX_TURNS = 150


def run_claude(
    prompt: str,
    *,
    timeout: int | None = 300,
    model: str | None = None,
    max_turns: int | None = DEFAULT_MAX_TURNS,
    disallowed_tools=DEFAULT_DISALLOWED_TOOLS,
) -> tuple[int, str, str]:
    """Run a `claude -p` subprocess and return (returncode, stdout, stderr).

    Parameters
    ----------
    prompt:
        Text sent to the claude CLI via stdin.
    timeout:
        Seconds before the subprocess is killed and a ``TimeoutExpired``
        exception is converted into a ``(-1, "", <message>)`` return value.
        Pass ``None`` for no timeout (unbounded wait).
    model:
        If given, passed as ``--model <model>``.  Must be a non-empty,
        non-whitespace string.
    max_turns:
        Passed as ``--max-turns``; ``None`` removes the ceiling.
    disallowed_tools:
        Permission rules passed as ``--disallowedTools``; the default denies
        ``git push``. Pass an empty sequence to send none.

    Returns
    -------
    tuple[int, str, str]
        ``(returncode, stdout, stderr)``.  On timeout ``returncode`` is
        ``-1`` and ``stderr`` contains a human-readable message.  On
        OS-level errors (binary not found, permission denied) ``returncode``
        is ``-2``.

    Cost emission is automatic on success — callers do not need to call
    emit_usage_xml.
    """
    if model is not None and not model.strip():
        raise ValueError(f"model must be a non-empty string, got {model!r}")

    cmd = ["claude", "-p", "-", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    if max_turns is not None:
        if int(max_turns) <= 0:
            raise ValueError(f"max_turns must be positive, got {max_turns!r}")
        cmd.extend(["--max-turns", str(int(max_turns))])
    rules = [r for r in (disallowed_tools or ()) if r]
    if rules:
        cmd.extend(["--disallowedTools", *rules])

    env = _build_subprocess_env()

    try:
        result = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        timeout_msg = f"claude subprocess timed out after {timeout}s"
        return -1, "", timeout_msg
    except (FileNotFoundError, PermissionError) as exc:
        return -2, "", f"could not launch claude subprocess: {exc}"

    stdout = result.stdout[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = result.stderr[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")

    if result.returncode == 0:
        try:
            emit_usage_xml(stdout)
        except Exception:
            # Cost emission failure must never discard a successful result.
            pass

    return result.returncode, stdout, stderr
