"""Tag/area inference for learnings — the one place scribe calls a model.

**Off by default.** Every ``notes.py learn`` without ``--tags`` used to spawn
a ``claude -p`` subprocess with a 10-second budget, and ``/wrapup`` writes its
learnings without tags — so a handoff paid for one model call per learning, on
the critical path, with a stderr warning on failure that nobody reads (review
§3 bug 10). A handoff now never spawns a model call unless it is asked to.

Three ways to ask, most specific first:

``--infer``               opt in for this one command
``--no-infer``            opt out for this one command, whatever the env says
``APIARY_SCRIBE_INFER=1`` opt in for the session (a ``/review-learnings``
                          pass, a batch import) without repeating the flag

``notes.py retrotag`` is the exception: inference *is* the command, so it
never consults the switch.

Failure is always soft. Any subprocess error, timeout, or unparseable reply
returns empty lists and the learning is still written — an untagged learning
is recoverable (``retrotag`` exists for exactly that), a lost one is not.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Set to 1/true/yes/on to infer tags for every `learn` in this session.
INFER_ENV_VAR = 'APIARY_SCRIBE_INFER'

#: Budget for a single inference during `learn`/`supersede`. Short: the user
#: is waiting, and an untagged learning is a perfectly good outcome.
DEFAULT_TIMEOUT = 10
#: Budget per learning during `retrotag`, where nobody is waiting on a prompt.
RETROTAG_TIMEOUT = 60

_TRUTHY = {'1', 'true', 'yes', 'on'}


def env_opt_in(environ=None) -> bool:
    """True when the session-wide opt-in env var is set to a truthy value."""
    env = os.environ if environ is None else environ
    return (env.get(INFER_ENV_VAR) or '').strip().lower() in _TRUTHY


def inference_enabled(args=None, *, environ=None) -> bool:
    """Whether this invocation may call a model.

    ``--no-infer`` wins over ``--infer`` wins over the env var wins over off.
    Explicit ``--tags``/``--area`` make the question moot; the caller checks
    that first, because supplied tags are never overridden by inferred ones.
    """
    if getattr(args, 'no_infer', False):
        return False
    if getattr(args, 'infer', False):
        return True
    return env_opt_in(environ)


def build_prompt(content: str, vocab: list) -> str:
    """The single-turn prompt sent to ``claude -p``.

    One copy: ``learn``, ``supersede`` and ``retrotag`` all send this, so a
    batch retrotag and a live learn produce the same shape of tag. (They were
    two near-identical copies before, in notes.py and a scripts/ one-shot.)
    """
    vocab_line = ', '.join(vocab) if vocab else '(none yet)'
    return (
        "You are tagging a project learning so it can be auto-surfaced when I later\n"
        "edit related files. Respond with a JSON object only — no prose, no markdown fence.\n\n"
        f"Existing tag vocabulary: {vocab_line}\n\n"
        'Return {"tags": [...], "areas": [...]} where:\n'
        '- tags: 1-3 short lowercase tokens (prefer existing vocabulary; invent only if needed).\n'
        '- areas: glob patterns matching file paths the learning applies to (e.g. "gui/**",\n'
        '  "scribe/notes.py", "core/hooks/*.py"). Empty list if not path-specific.\n\n'
        f"Learning content:\n{content}"
    )


def parse_response(stdout: str) -> "dict | None":
    """Pull the ``{"tags": [...], "areas": [...]}`` payload out of a reply.

    Unwraps a ``claude -p --output-format json`` envelope and a markdown
    fence if either is present. Returns ``None`` on any parse failure so the
    caller can fall back rather than write half-parsed tags.
    """
    try:
        envelope = json.loads(stdout)
        inner = envelope.get('result', stdout) if isinstance(envelope, dict) else stdout
    except json.JSONDecodeError:
        inner = stdout
    if not isinstance(inner, str):
        return None
    text = inner.strip()
    fence = re.search(r'```(?:json)?\s*\n([\s\S]*?)```', text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def normalize(parsed: dict) -> dict:
    """Coerce a parsed reply to ``{'tags': [str], 'areas': [str]}``, dropping blanks."""
    return {
        'tags': [str(t).strip() for t in (parsed.get('tags') or []) if str(t).strip()],
        'areas': [str(a).strip() for a in (parsed.get('areas') or []) if str(a).strip()],
    }


def collect_vocab(store) -> list:
    """The tags already in use across a store's learnings, sorted.

    Fed to the prompt so the model reuses the existing vocabulary instead of
    inventing a synonym for a tag that already exists.
    """
    try:
        return sorted({
            t.strip() for entry in store.list_learnings()
            for t in (entry.get('tags') or [])
            if isinstance(t, str) and t.strip()
        })
    except Exception:
        return []


def _warn(message: str) -> None:
    print(f'warning: {message}', file=sys.stderr)


def infer_tags_areas(content: str, store, *, model: "str | None" = None,
                     timeout: int = DEFAULT_TIMEOUT, vocab: "list | None" = None,
                     warn=_warn) -> dict:
    """Ask a model for tags and areas. Returns ``{}`` on any failure.

    ``runner.claude_subprocess`` is imported lazily: it pulls in the runner
    package, and this module is imported by ``notes.py`` on every invocation
    including the ones that never infer.
    """
    try:
        from runner.claude_subprocess import run_claude
    except Exception as e:  # runner missing or broken — not worth failing a write
        warn(f'tag/area inference unavailable ({e})')
        return {}

    prompt = build_prompt(content, collect_vocab(store) if vocab is None else vocab)
    rc, stdout, stderr = run_claude(prompt, timeout=timeout, model=model)
    if rc != 0 or not stdout.strip():
        warn(f'tag/area inference failed ({stderr[:200] if stderr else "empty output"})')
        return {}
    parsed = parse_response(stdout)
    if parsed is None:
        warn('tag/area inference returned unparseable JSON')
        return {}
    return normalize(parsed)
