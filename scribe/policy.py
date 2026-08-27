"""Scribe retention policy — what auto-archives, and when.

The rules live here rather than in the CLI so every caller applies the same
ones: ``notes.py add`` (after each write), ``notes.py tidy`` (on demand) and
``core/startup.py`` (once per session). Before this module the policy sat in
``scribe/notes.py`` and ``core/startup.py`` had to import a CLI to get it.

Everything above :func:`run_auto_archive` is a pure function over index rows
— the dicts ``ScribeStore.list_notes`` returns — so the rules are testable
without a store, a temp dir, or a clock.

The rules, by note type:

============  ==========================================================
handoff       keep only the newest per ``(role, mission)``; archive the rest
context       archive after 3 days (mid-session checkpoints decay fast)
decision      archive after 30 days (historical record, not live state)
done          archive 1 day after the note was *marked* done
todo/wishlist keep until closed
blocker       keep until closed
reference     keep until closed
general       keep until closed
============  ==========================================================

The "done" clock reads ``status_changed_at`` — stamped by ``update_note`` on
every status transition — and falls back to ``timestamp`` only for rows
written before that field existed. Measuring from creation instead archived
notes the moment they were closed, which is what made them vanish out from
under a follow-up ``update`` (review §3 bug 3).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.utils.timeutil import parse_iso

#: Mid-session checkpoints decay fast.
CONTEXT_RETENTION_DAYS = 3
#: A decision is a historical record; it stops being live state after a month.
DECISION_RETENTION_DAYS = 30
#: Grace period after a note is *marked* done, so a follow-up edit still lands.
DONE_RETENTION_DAYS = 1
#: Cutoff `notes.py archive` uses when no ``--before`` date is given.
DEFAULT_ARCHIVE_DAYS = 30

#: ``(role, mission)`` defaults for a row that carries neither.
_DEFAULT_OWNER = ("user", "general")

#: One entry per type with a plain age rule: type name -> retention days.
_AGE_RULES: dict[str, int] = {
    "context": CONTEXT_RETENTION_DAYS,
    "decision": DECISION_RETENTION_DAYS,
}


def note_key(row: dict) -> tuple:
    """Return the ``(type, year, seq)`` triple that addresses a note."""
    return (row.get("type"), row.get("year"), row.get("seq"))


def handoff_owner(row: dict) -> tuple:
    """Return the ``(role, mission)`` a handoff belongs to.

    Handoffs are retained per owner, not globally: an ``attacker``/``harden``
    session's handoff must not evict the ``user``/``general`` one.
    """
    return (row.get("role", _DEFAULT_OWNER[0]), row.get("mission", _DEFAULT_OWNER[1]))


def latest_handoffs(rows) -> dict:
    """Map ``(role, mission)`` → the newest handoff timestamp among *rows*.

    Rows that are not handoffs, and handoffs with an unparseable timestamp,
    are ignored — an unparseable row can neither win nor be evicted.
    """
    latest: dict[tuple, datetime] = {}
    for row in rows:
        if row.get("type") != "handoff":
            continue
        ts = parse_iso(row.get("timestamp"))
        if ts is None:
            continue
        key = handoff_owner(row)
        if key not in latest or ts > latest[key]:
            latest[key] = ts
    return latest


def done_at(row: dict) -> "datetime | None":
    """When *row* was marked done, falling back to when it was created.

    The fallback is for index rows written before ``status_changed_at``
    existed; without it every legacy done note would look freshly closed and
    never age out.
    """
    return parse_iso(row.get("status_changed_at")) or parse_iso(row.get("timestamp"))


def should_auto_archive(row: dict, *, now: datetime, latest_handoff: dict) -> bool:
    """True when *row* has aged past its type's retention rule.

    *latest_handoff* is the map :func:`latest_handoffs` builds over the same
    row set — passed in rather than recomputed so a sweep over N rows stays
    O(N) instead of O(N²).

    A done note that has not yet served its grace period still falls through
    to its type's own age rule, so closing a month-old decision does not
    reset its clock.
    """
    created = parse_iso(row.get("timestamp"))
    if created is None:
        # No usable clock: leave it alone rather than archive on a guess.
        return False
    if row.get("status") == "done":
        closed = done_at(row) or created
        if closed < now - timedelta(days=DONE_RETENTION_DAYS):
            return True
    note_type = row.get("type", "")
    if note_type == "handoff":
        return created < latest_handoff.get(handoff_owner(row), created)
    days = _AGE_RULES.get(note_type)
    return days is not None and created < now - timedelta(days=days)


def select_auto_archive(rows, *, now: "datetime | None" = None) -> list:
    """Return the ``(type, year, seq)`` keys of *rows* due for auto-archive.

    Pure: takes index rows, returns keys, touches no disk. Order follows the
    input so a caller's report reads in the same order it listed the notes.
    """
    now = now or datetime.now(timezone.utc)
    latest_handoff = latest_handoffs(rows)
    return [
        note_key(row)
        for row in rows
        if should_auto_archive(row, now=now, latest_handoff=latest_handoff)
    ]


def select_archivable_before(rows, cutoff: datetime) -> list:
    """Return the keys of *rows* older than *cutoff* that `archive` may move.

    The manual ``notes.py archive [--before]`` sweep is deliberately narrower
    than the automatic one: only done notes and handoffs, and only on age.
    Live todos and blockers are never swept by date.
    """
    keys = []
    for row in rows:
        created = parse_iso(row.get("timestamp"))
        if created is None or created >= cutoff:
            continue
        if row.get("status") == "done" or row.get("type") == "handoff":
            keys.append(note_key(row))
    return keys


def default_archive_cutoff(now: "datetime | None" = None) -> datetime:
    """The ``--before`` date `notes.py archive` uses when none is supplied."""
    return (now or datetime.now(timezone.utc)) - timedelta(days=DEFAULT_ARCHIVE_DAYS)


def external_ticket_links(note: dict) -> list:
    """The external canonical-ticket refs (``ticket:K-<id>`` tags) on a note.

    These name tickets owned by the external Asana tool, not apiary. Marking
    the linked todo done is the mirror→canonical *close signal*; apiary holds
    no local ``K`` tickets, so it takes no local action. Only ``K``-prefixed
    refs are returned — apiary NEVER cascades ``done`` into a local note (spec
    §5.14 / A2 hazard guard) — and the external id is never parsed, so a
    missing or unparseable ticket cannot raise.
    """
    refs = []
    for tag in note.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("ticket:"):
            ref = tag.split(":", 1)[1].strip()
            if ref.upper().startswith("K-"):
                refs.append(ref)
    return refs


def run_auto_archive(store, *, now: "datetime | None" = None) -> int:
    """Apply the retention rules to *store*. Returns the number archived.

    The one impure function in the module, and the one every caller uses:
    ``add`` runs it after each write, ``tidy`` on demand, ``core/startup.py``
    once per session.
    """
    keys = select_auto_archive(store.list_notes(status="active"), now=now)
    archived = 0
    for note_type, year, seq in keys:
        if store.archive_note(note_type, year, seq) is not None:
            archived += 1
    return archived
