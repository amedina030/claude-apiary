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

The startup hooks (core/hooks/startup_prompt_hook.py and
core/hooks/startup_hook.py) check the env var on entry and short-circuit
to a no-context allow when it is set.

Module-level constant
---------------------
RUNNER_SUBPROCESS_ENV_VAR : str
    Name of the environment variable set in every spawned subprocess.
    Import this constant from here rather than re-defining it in hooks so
    both sides stay in sync automatically.
"""
import os
import subprocess

from cost_emit import emit_usage_xml

RUNNER_SUBPROCESS_ENV_VAR = "APIARY_RUNNER_SUBPROCESS"

# Maximum bytes buffered from stdout/stderr before we truncate.  Prevents
# unbounded memory growth for unexpectedly large subprocess outputs.
_MAX_OUTPUT_BYTES = 50 * 1024 * 1024  # 50 MB


def run_claude(
    prompt: str,
    *,
    timeout: int | None = 300,
    model: str | None = None,
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

    env = {**os.environ, RUNNER_SUBPROCESS_ENV_VAR: "1"}

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
