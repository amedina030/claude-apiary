"""One UTC timestamp format for apiary's on-disk state.

``%Y-%m-%dT%H:%M:%SZ`` — second precision, explicit ``Z``, no offset and
no microseconds — because these strings are sorted as text (compass sorts
observations newest-first by ``captured_at``; the registry's ``last_used``
is compared as a string), and a mix of ``+00:00`` and ``Z`` forms sorts
wrong. Three copies of this ``strftime`` call existed before this module
(review finding X-3).

Timestamps that are *not* "now" (a file mtime, a parsed value) should use
:data:`ISO_FORMAT` rather than re-spelling the format string.
"""
from __future__ import annotations

from datetime import datetime, timezone

#: The one on-disk timestamp format. See the module docstring for why.
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(ts) -> "datetime | None":
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    Returns ``None`` for anything unparseable — a missing key, ``None``, a
    non-string, or a malformed value — because every caller here is reading
    a hand-editable on-disk row and would otherwise have to guard each read.
    The reader half of :data:`ISO_FORMAT`: this is the one place the ``Z`` →
    ``+00:00`` substitution lives (it existed three times before — scribe's
    retention sweep, scribe's age formatter, and the frozen scribe API).
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
