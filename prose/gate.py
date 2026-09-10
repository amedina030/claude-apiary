"""The prose gate: the ``(message, fatal)`` contract scribe and the hook share.

Mirrors ``scribe.templates.gate``: the caller prints ``message`` to stderr
when it is not None and stops when ``fatal`` is true. Blocking is off until
the per-repo ``prose-gate`` flag is on (``core/flags.py enable prose-gate``),
so every surface starts advisory: findings are shown, the write proceeds.
Once blocking is on, an ``error``-level finding stops the write unless the
caller passes ``force``, in which case the note is tagged ``prose-forced`` so
bypasses can be listed later. Warnings and suggestions never block.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prose import engine

FLAG = "prose-gate"
FORCED_TAG = "prose-forced"
STANDARD_DOC = "docs/standards/prose-style.md"
#: A message starting with this means --force waved an error-level finding
#: through; the caller tags the note ``FORCED_TAG``.
BYPASS_PREFIX = "[prose gate bypassed via --force"

READ_REMINDER = "Aphoristic closers, mirror constructions and reflexive triads need a read; a clean report is necessary, not sufficient."


def forced(message: "str | None") -> bool:
    """True when *message* (from :func:`gate`) records a --force bypass."""
    return bool(message) and message.startswith(BYPASS_PREFIX)


def blocking_enabled() -> bool:
    """The per-repo flag, false when no bootstrapped repo is in scope."""
    try:
        from core import flags

        return flags.is_enabled(FLAG)
    except Exception:  # noqa: BLE001 — a flag lookup must never break a note write
        return False


def gate(
    text: str,
    *,
    force: bool = False,
    blocking: "bool | None" = None,
    min_level: str = "warning",
    config: "dict | None" = None,
    label: str = "prose",
) -> "tuple[str | None, bool]":
    """Check *text*. Returns ``(message, fatal)``; ``(None, False)`` when clean."""
    config = config or engine.load_config()
    findings = engine.filter_level(engine.check_text(text, config=config), min_level)
    if not findings:
        return (None, False)
    if blocking is None:
        blocking = blocking_enabled()
    errors = engine.has_level(findings, "error")
    body = engine.format_text(findings, path=label)

    if blocking and errors and not force:
        tail = (
            f"Rewrite and re-run, or pass --force to bypass (the note is tagged {FORCED_TAG}).\n"
            f"See {STANDARD_DOC}. {READ_REMINDER}"
        )
        return (f"{body}\n{tail}", True)
    if blocking and errors and force:
        c = engine.counts(findings)
        return (f"{BYPASS_PREFIX}: {c['error']} error(s)]\n{body}", False)
    tail = f"Advisory ({FLAG} flag off): saved as-is. See {STANDARD_DOC}. {READ_REMINDER}"
    if blocking:
        tail = f"No error-level finding; saved. See {STANDARD_DOC}. {READ_REMINDER}"
    return (f"{body}\n{tail}", False)
