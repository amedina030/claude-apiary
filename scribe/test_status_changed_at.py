"""Status-transition timestamp behavior (parity spec §5.6).

Adapted from the source scribe oracle ``test_status_changed_at``: a status
change stamps ``status_changed_at``; a content-only update does not.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


if __name__ == "__main__":
    unittest.main()
