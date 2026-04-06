"""Shared `claude -p` subprocess runner for pipeline stages.

Centralizes the spawn so every pipeline subprocess:
  1. Sets ``APIARY_PIPELINE_SUBPROCESS=1`` in its env, telling startup hooks
     in the spawned Claude Code session to skip context injection (identity,
     active notes summary, full learnings dump, CLI tools index). These
     payloads are useful to interactive sessions but pure overhead for a
     one-shot pipeline worker — typically tens of KB of input tokens that
     the worker doesn't read.
  2. Emits a ``<usage>`` XML block via cost_emit so run.py can attribute
     per-stage tokens.

The startup hooks (core/hooks/startup_prompt_hook.py and
core/hooks/startup_hook.py) check the env var on entry and short-circuit
to a no-context allow when it is set.
"""
import os
import subprocess

from cost_emit import emit_usage_xml

PIPELINE_SUBPROCESS_ENV_VAR = "APIARY_PIPELINE_SUBPROCESS"


def run_claude(
    prompt: str,
    *,
    timeout: int = 300,
    model: str | None = None,
) -> tuple[int, str, str]:
    """Run a `claude -p` subprocess and return (returncode, stdout, stderr).

    Cost emission is automatic — callers do not need to call emit_usage_xml.
    """
    cmd = ["claude", "-p", "-", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])

    env = {**os.environ, PIPELINE_SUBPROCESS_ENV_VAR: "1"}

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    emit_usage_xml(result.stdout)
    return result.returncode, result.stdout, result.stderr
