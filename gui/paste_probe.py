"""Temporary paste-truncation diagnostics — DELETE once the layer is found.

Appends one line per hop to ``<state-dir>/paste_probe.log`` so a single paste
can be traced across browser -> bridge -> pty. Each hop records the length it
*observed* plus head/tail fingerprints; comparing lengths across hops pinpoints
the layer that drops bytes. All calls are best-effort and never raise into the
hot send path.

Read the log after a paste test:
    python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" --print-repo-path  # for context
    # then: <main-apiary>/.apiary/gui/apiary_gui/paste_probe.log
"""

from __future__ import annotations

import time

from gui import paths

_LOG_NAME = "paste_probe.log"


def _fingerprint(s: str, n: int = 24) -> str:
    """First/last n chars as a repr, so truncation is visible at a glance."""
    if len(s) <= 2 * n:
        return repr(s)
    return f"{s[:n]!r}…{s[-n:]!r}"


def probe(hop: str, length: int, head: str = "", tail: str = "") -> None:
    """Append one probe line. ``length`` is the hop's own measured length;
    ``head``/``tail`` are short fingerprints (already sliced by the caller for
    the browser hop, or derived here for Python hops via :func:`probe_text`).
    """
    try:
        line = (
            f"{time.time():.3f} hop={hop} len={length} "
            f"head={head!r} tail={tail!r}\n"
        )
        path = paths.state_dir() / _LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def probe_text(hop: str, text: str, n: int = 24) -> None:
    """Convenience for Python hops: measure ``text`` and log head/tail."""
    if not isinstance(text, str):
        return
    head = text[:n]
    tail = text[-n:] if len(text) > n else ""
    probe(hop, len(text), head, tail)
