#!/usr/bin/env python3
"""Shared plumbing for the runner's LLM stages.

Before this module the same four jobs were reimplemented once per stage
(review X-3, `git show 5b95eaa:docs/review/subsystems/runner.md` §2):

* ``run_claude`` — 5 copies (auto_refine, auto_plan, executor, auto_harden,
  approval), each with its own timeout/model lookup.
* Envelope / fence / prose JSON extraction — 5 copies, **all different**, so
  a salvage fix in one stage never reached the other four.
* The LLM retry loop (attempt → spawn → parse → validate → keep the best
  attempt) — 2 near-identical ~70-line bodies in auto_refine and auto_plan.
* The UUID path-traversal guard — 6 copies.

There is one of each here. Stages keep their thin wrappers only where the
wrapper carries stage-specific config (which model, which timeout).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

# The single spawn point. Re-exported so a stage imports one name from one
# place instead of writing its own deferred-import wrapper.
from .claude_subprocess import run_claude  # noqa: F401  (re-export)

_FENCE_RE = re.compile(r"```(?:json)?\s*\n([\s\S]*?)```")


class ClaudeMissingError(RuntimeError):
    """The `claude` CLI could not be launched at all.

    Distinct from a failed call: retrying is pointless, so ``retry_until_valid``
    raises instead of burning the remaining attempts.
    """


# --------------------------------------------------------------------------- #
# UUID / slug safety
# --------------------------------------------------------------------------- #


def is_uuid_safe(value: Any) -> bool:
    """True if *value* is a plain filename component safe to interpolate.

    Rejects non-strings, empties, NUL bytes, ``.``/``..``, and anything with a
    path separator — including a backslash on POSIX, where
    ``Path("a\\b").name`` is the whole string and the Path comparison alone
    would let it through.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    if "\\" in value or "\x00" in value or value in (".", ".."):
        return False
    if Path(value) != Path(Path(value).name):
        return False
    return bool(Path(value).name)


def check_uuid_safe(value: Any, label: str = "uuid") -> str:
    """Return the stripped *value*, or raise ``ValueError`` if it is unsafe."""
    if not isinstance(value, str):
        raise ValueError(f"{label} field is not a string")
    if not is_uuid_safe(value):
        raise ValueError(f"{label} field contains invalid characters (path separators not allowed)")
    return value.strip()


# --------------------------------------------------------------------------- #
# JSON salvage
# --------------------------------------------------------------------------- #


def sanitize_json_newlines(text: str) -> str:
    """Escape literal newlines/tabs inside JSON string values.

    LLMs routinely emit JSON with raw newlines inside multi-line string
    fields (``code_spec`` above all). This walks the text tracking whether we
    are inside a string and escapes what would otherwise be a parse error.
    """
    result = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and in_string:
            # Escaped character — pass through both chars
            result.append(ch)
            if i + 1 < len(text):
                i += 1
                result.append(text[i])
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
        if ch == "\n" and in_string:
            result.append("\\n")
        elif ch == "\r" and in_string:
            pass  # drop \r, the \n that follows will be escaped
        elif ch == "\t" and in_string:
            result.append("\\t")
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def extract_text(raw_output: str) -> str:
    """Unwrap the ``claude -p --output-format json`` envelope.

    Returns ``envelope["result"]`` when the output is that envelope, else the
    input unchanged.
    """
    try:
        envelope = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        return raw_output
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
        return envelope["result"]
    return raw_output


def _strip_fence(text: str) -> str:
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _scan(candidate: str, openers: str, decoder: json.JSONDecoder):
    """Yield every JSON value that starts at an opener in *candidate*."""
    for i, ch in enumerate(candidate):
        if ch not in openers:
            continue
        try:
            obj, _end = decoder.raw_decode(candidate, i)
        except json.JSONDecodeError:
            continue
        yield obj


def extract_json(
    raw_output: str,
    *,
    require_keys: Sequence[str] = (),
    allow_list: bool = True,
) -> Any:
    """Pull the first usable JSON value out of a stage's stdout.

    Handles, in order: the Claude Code envelope, a bare JSON value, markdown
    fences (including one the model opened and never closed), JSON embedded in
    prose, and unescaped newlines inside string values.

    ``require_keys`` names the keys that identify the *wanted* object: any
    candidate carrying all of them wins outright, and only if none does is the
    first parseable value returned instead. ``allow_list`` controls whether a
    top-level array counts as a candidate (the harden validators want arrays;
    the spec/plan parsers only ever want an object).

    Raises ``json.JSONDecodeError`` when nothing parses.
    """

    def _wanted(value: Any) -> bool:
        return isinstance(value, dict) and all(key in value for key in require_keys)

    text = raw_output if isinstance(raw_output, str) else ""
    try:
        envelope = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        envelope = None
    if envelope is not None:
        if _wanted(envelope):
            return envelope
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            text = envelope["result"]
        elif isinstance(envelope, list) and allow_list:
            return envelope
        elif isinstance(envelope, dict) and not require_keys:
            return envelope

    text = _strip_fence(text)
    openers = "{[" if allow_list else "{"
    decoder = json.JSONDecoder()
    candidates = [sanitize_json_newlines(text), text]

    if require_keys:
        for candidate in candidates:
            for value in _scan(candidate, openers, decoder):
                if _wanted(value):
                    return value
    for candidate in candidates:
        for value in _scan(candidate, openers, decoder):
            if allow_list or isinstance(value, dict):
                return value

    raise json.JSONDecodeError("No valid JSON found in output", text, 0)


def extract_json_str(raw_output: str, *, allow_list: bool = True) -> str:
    """``extract_json`` re-serialized to canonical JSON text.

    The harden/approval stages feed the result to a validator subprocess, so
    they want a string — and re-serializing drops the trailing prose or second
    JSON block the model sometimes appends. Returns the stripped input when
    nothing parses, matching the old per-stage behaviour.
    """
    try:
        return json.dumps(extract_json(raw_output, allow_list=allow_list))
    except json.JSONDecodeError:
        return (raw_output or "").strip()


# --------------------------------------------------------------------------- #
# The validate → retry loop
# --------------------------------------------------------------------------- #


def retry_until_valid(
    *,
    build_prompt: Callable[[Optional[list], Optional[dict]], str],
    call_model: Callable[[str], tuple],
    parse: Callable[[str], Any],
    assemble: Callable[[Any], dict],
    persist: Callable[[dict], None],
    validate: Callable[[], list],
    max_attempts: int = 3,
    report: Callable[[str], None] = lambda msg: None,
) -> tuple[bool, Optional[dict], list]:
    """Drive one LLM stage until its deterministic validator is happy.

    The loop auto_refine and auto_plan each had their own copy of: build the
    prompt (carrying the previous attempt's validator errors and, when one
    exists, the previous attempt's artifact -- so the model minimally edits
    its own output instead of regenerating and losing fixes that already
    passed), spawn the
    model, salvage JSON, assemble the artifact, write it, validate it — and on
    exhaustion keep the attempt with the fewest errors so an operator has
    something to read.

    Returns ``(ok, artifact, errors)``. On success *artifact* is the accepted
    one and *errors* is empty; on exhaustion *artifact* is the best attempt
    (already persisted) and *errors* are its validator errors. Raises
    ``ClaudeMissingError`` if the CLI cannot be launched.
    """
    best_artifact: Optional[dict] = None
    best_errors: Optional[list] = None
    previous_errors: Optional[list] = None
    previous_artifact: Optional[dict] = None

    for attempt in range(1, max_attempts + 1):
        report(f"Attempt {attempt}/{max_attempts}...")
        prompt = build_prompt(previous_errors, previous_artifact)

        try:
            returncode, stdout, stderr = call_model(prompt)
        except subprocess.TimeoutExpired:
            report(f"Claude Code error: subprocess timed out (attempt {attempt})")
            previous_errors = ["Claude Code subprocess timed out"]
            continue
        except FileNotFoundError:
            raise ClaudeMissingError("Claude Code error: 'claude' command not found")

        if returncode != 0:
            msg = (stderr or "").strip() or f"exit code {returncode}"
            report(f"Claude Code error: {msg} (attempt {attempt})")
            previous_errors = [f"Claude Code failed: {msg}"]
            continue

        try:
            parsed = parse(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            report(f"Failed to parse JSON (attempt {attempt}): {exc}")
            previous_errors = [f"Output was not valid JSON: {exc}"]
            continue

        artifact = assemble(parsed)
        persist(artifact)

        errors = validate()
        if not errors:
            return True, artifact, []

        if best_errors is None or len(errors) < len(best_errors):
            best_artifact = artifact
            best_errors = errors

        previous_errors = errors
        previous_artifact = artifact
        report(f"Validation failed (attempt {attempt}): {len(errors)} error(s)")
        for err in errors:
            report(f"  {err}")

    return False, best_artifact, list(best_errors or [])


def run_validator(module: str, path: Path, *, cwd: Path) -> list:
    """Run a deterministic validator module and return its error lines.

    Empty list means valid. Every LLM stage re-invokes its validator as a
    subprocess so the validator's own argv contract is the thing being
    tested, not an in-process import that could drift from it.
    """
    import sys

    result = subprocess.run(
        [sys.executable, "-m", module, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
    )
    if result.returncode == 0:
        return []
    return [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]


def iter_unique(values: Iterable[str]) -> list:
    """Order-preserving de-duplication of a string sequence."""
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
