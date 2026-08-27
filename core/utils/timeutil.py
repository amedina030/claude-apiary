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
