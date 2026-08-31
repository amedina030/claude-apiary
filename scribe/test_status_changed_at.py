"""Status-transition timestamp behavior (parity spec §5.6).

Adapted from the source scribe oracle ``test_status_changed_at``: a status
change stamps ``status_changed_at``; a content-only update does not.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.utils.timeutil import parse_iso
from scribe import policy
from scribe.store import ScribeStore


class TestStatusChangedAt(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_new_note_has_no_status_changed_at(self):
        a = self.store.add_note("todo", "x", session_id="s")
        self.assertNotIn("status_changed_at", a)

    def test_status_change_sets_timestamp(self):
        a = self.store.add_note("todo", "x", session_id="s")
        updated = self.store.update_note("todo", a["year"], a["seq"], status="done")
        self.assertIn("status_changed_at", updated)
        self.assertTrue(updated["status_changed_at"])

    def test_content_update_does_not_set_status_changed_at(self):
        a = self.store.add_note("todo", "orig", session_id="s")
        self.store.update_note("todo", a["year"], a["seq"], content="new body")
        fetched = self.store.get_note("todo", a["year"], a["seq"])
        self.assertNotIn("status_changed_at", fetched)

    def test_each_transition_restamps(self):
        a = self.store.add_note("todo", "x", session_id="s")
        u1 = self.store.update_note("todo", a["year"], a["seq"], status="deferred")
        u2 = self.store.update_note("todo", a["year"], a["seq"], status="active")
        self.assertGreaterEqual(u2["status_changed_at"], u1["status_changed_at"])


class TestExplicitStatusChangedAt(unittest.TestCase):
    """An explicitly passed status_changed_at wins over the automatic stamp."""

    EXPLICIT = "2026-01-02T03:04:05+00:00"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _add(self):
        return self.store.add_note("todo", "x", session_id="s")

    def _assert_near_now(self, value):
        self.assertTrue(value)
        parsed = parse_iso(value)
        self.assertIsNotNone(parsed)
        self.assertLess(abs((datetime.now(timezone.utc) - parsed).total_seconds()), 60)

    def test_explicit_status_changed_at_is_kept(self):
        a = self._add()
        u = self.store.update_note(
            "todo", a["year"], a["seq"], status="done", status_changed_at=self.EXPLICIT
        )
        self.assertEqual(u["status_changed_at"], self.EXPLICIT)
        fetched = self.store.get_note("todo", a["year"], a["seq"])
        self.assertEqual(fetched["status_changed_at"], self.EXPLICIT)

    def test_none_status_changed_at_gets_fresh_stamp(self):
        a = self._add()
        u = self.store.update_note(
            "todo", a["year"], a["seq"], status="done", status_changed_at=None
        )
        self._assert_near_now(u["status_changed_at"])

    def test_empty_status_changed_at_gets_fresh_stamp(self):
        a = self._add()
        u = self.store.update_note("todo", a["year"], a["seq"], status="done", status_changed_at="")
        self._assert_near_now(u["status_changed_at"])

    def test_status_without_explicit_timestamp_gets_fresh_stamp(self):
        a = self._add()
        u = self.store.update_note("todo", a["year"], a["seq"], status="done")
        self._assert_near_now(u["status_changed_at"])

    def test_unparseable_explicit_value_is_stored_verbatim(self):
        a = self._add()
        u = self.store.update_note(
            "todo", a["year"], a["seq"], status="done", status_changed_at="not-a-date"
        )
        self.assertEqual(u["status_changed_at"], "not-a-date")
        self.assertIsNotNone(policy.done_at(u))
        self.assertEqual(policy.done_at(u), parse_iso(u["timestamp"]))

    def test_missing_seq_returns_none_and_leaves_index_untouched(self):
        a = self._add()
        index = self.store._type_dir("todo") / str(a["year"]) / "index.jsonl"
        before = index.read_text(encoding="utf-8")
        result = self.store.update_note(
            "todo", a["year"], 999, status="done", status_changed_at=self.EXPLICIT
        )
        self.assertIsNone(result)
        self.assertEqual(index.read_text(encoding="utf-8"), before)

    def test_backdating_idiom_still_archives(self):
        a = self._add()
        self.store.update_note("todo", a["year"], a["seq"], status="done")
        stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        u = self.store.update_note("todo", a["year"], a["seq"], status_changed_at=stale)
        self.assertEqual(u["status_changed_at"], stale)
        self.assertEqual(policy.run_auto_archive(self.store), 1)

    def test_explicit_value_wins_on_archived_note(self):
        a = self._add()
        self.store.update_note("todo", a["year"], a["seq"], status="done")
        stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.store.update_note("todo", a["year"], a["seq"], status_changed_at=stale)
        self.assertEqual(policy.run_auto_archive(self.store), 1)
        u = self.store.update_note(
            "todo", a["year"], a["seq"], status="active", status_changed_at=self.EXPLICIT
        )
        self.assertEqual(u["status_changed_at"], self.EXPLICIT)
        self.assertIs(u["_from_archive"], True)

    def test_future_timestamp_keeps_note_active(self):
        a = self._add()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        u = self.store.update_note(
            "todo", a["year"], a["seq"], status="done", status_changed_at=future
        )
        self.assertEqual(u["status_changed_at"], future)
        self.assertEqual(policy.run_auto_archive(self.store), 0)
        fetched = self.store.get_note("todo", a["year"], a["seq"])
        self.assertNotIn("_from_archive", fetched)


if __name__ == "__main__":
    unittest.main()
