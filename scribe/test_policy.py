#!/usr/bin/env python3
"""Tests for scribe/policy.py — the retention rules as pure functions.

These need no store and no temp dir: every rule takes index rows and a clock
and returns keys. The store-level sweep is covered once at the bottom.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scribe import policy
from scribe.store import ScribeStore

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def row(**kwargs) -> dict:
    """An index row with sane defaults; override what the test is about."""
    base = {
        "type": "todo",
        "year": 2026,
        "seq": 1,
        "status": "active",
        "timestamp": NOW.isoformat(),
    }
    base.update(kwargs)
    return base


def ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


class TestSelectAutoArchive(unittest.TestCase):
    def select(self, rows):
        return policy.select_auto_archive(rows, now=NOW)

    def test_fresh_notes_are_kept(self):
        self.assertEqual(self.select([row(), row(type="context", seq=2)]), [])

    def test_todo_is_never_archived_on_age_alone(self):
        self.assertEqual(self.select([row(timestamp=ago(days=900))]), [])

    def test_context_ages_out_after_three_days(self):
        rows = [
            row(type="context", seq=1, timestamp=ago(days=4)),
            row(type="context", seq=2, timestamp=ago(days=2)),
        ]
        self.assertEqual(self.select(rows), [("context", 2026, 1)])

    def test_decision_ages_out_after_thirty_days(self):
        rows = [
            row(type="decision", seq=1, timestamp=ago(days=31)),
            row(type="decision", seq=2, timestamp=ago(days=29)),
        ]
        self.assertEqual(self.select(rows), [("decision", 2026, 1)])

    def test_done_ages_from_status_changed_at(self):
        rows = [row(status="done", timestamp=ago(days=90), status_changed_at=NOW.isoformat())]
        self.assertEqual(self.select(rows), [], "just-closed note must survive")

    def test_done_two_days_ago_is_archived(self):
        rows = [row(status="done", timestamp=ago(days=90), status_changed_at=ago(days=2))]
        self.assertEqual(self.select(rows), [("todo", 2026, 1)])

    def test_legacy_done_row_falls_back_to_timestamp(self):
        self.assertEqual(
            self.select([row(status="done", timestamp=ago(days=5))]), [("todo", 2026, 1)]
        )

    def test_fresh_done_still_falls_through_to_its_type_rule(self):
        # Closing a month-old decision must not reset its retention clock.
        rows = [
            row(
                type="decision",
                status="done",
                timestamp=ago(days=40),
                status_changed_at=NOW.isoformat(),
            )
        ]
        self.assertEqual(self.select(rows), [("decision", 2026, 1)])

    def test_only_the_latest_handoff_survives(self):
        rows = [
            row(type="handoff", seq=1, timestamp=ago(days=2)),
            row(type="handoff", seq=2, timestamp=ago(hours=1)),
            row(type="handoff", seq=3, timestamp=ago(days=9)),
        ]
        self.assertEqual(sorted(self.select(rows)), [("handoff", 2026, 1), ("handoff", 2026, 3)])

    def test_handoffs_are_retained_per_role_and_mission(self):
        rows = [
            row(type="handoff", seq=1, timestamp=ago(days=2), role="user"),
            row(type="handoff", seq=2, timestamp=ago(hours=1), role="user"),
            row(type="handoff", seq=3, timestamp=ago(days=5), role="attacker"),
        ]
        # The attacker's only handoff is the latest for its own owner.
        self.assertEqual(self.select(rows), [("handoff", 2026, 1)])

    def test_unparseable_timestamp_is_left_alone(self):
        self.assertEqual(
            self.select(
                [
                    row(type="context", timestamp="not a date"),
                    row(type="context", seq=2, timestamp=None),
                ]
            ),
            [],
        )

    def test_a_lone_handoff_is_never_its_own_eviction(self):
        self.assertEqual(self.select([row(type="handoff", timestamp=ago(days=400))]), [])


class TestSelectArchivableBefore(unittest.TestCase):
    """`notes.py archive [--before]` is narrower than the automatic sweep."""

    def select(self, rows, days=30):
        return policy.select_archivable_before(rows, NOW - timedelta(days=days))

    def test_old_done_and_handoffs_only(self):
        rows = [
            row(seq=1, status="done", timestamp=ago(days=40)),
            row(seq=2, type="handoff", timestamp=ago(days=40)),
            row(seq=3, timestamp=ago(days=40)),  # open todo
            row(seq=4, status="done", timestamp=ago(days=2)),
        ]
        self.assertEqual(self.select(rows), [("todo", 2026, 1), ("handoff", 2026, 2)])

    def test_default_cutoff_is_thirty_days_back(self):
        cutoff = policy.default_archive_cutoff(NOW)
        self.assertEqual((NOW - cutoff).days, policy.DEFAULT_ARCHIVE_DAYS)


class TestRunAutoArchive(unittest.TestCase):
    """The one impure function: it must move exactly what select() picks."""

    def test_sweeps_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp).resolve())
            stale = store.add_note("todo", "closed a while back", "s1")
            store.update_note("todo", stale["year"], stale["seq"], status="done")
            store.update_note(
                "todo",
                stale["year"],
                stale["seq"],
                status_changed_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            )
            keep = store.add_note("todo", "still open", "s1")

            self.assertEqual(policy.run_auto_archive(store), 1)
            self.assertTrue(
                store.get_note("todo", stale["year"], stale["seq"]).get("_from_archive")
            )
            self.assertFalse(store.get_note("todo", keep["year"], keep["seq"]).get("_from_archive"))
            # Idempotent: a second sweep has nothing left to move.
            self.assertEqual(policy.run_auto_archive(store), 0)


if __name__ == "__main__":
    unittest.main()
