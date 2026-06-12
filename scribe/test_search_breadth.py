"""Widened search over metadata + body (parity spec §5.6/§5.7).

Notes search matches display_id, summary, brief_summary, session, tags, and
the body; learnings search matches summary, tags, and body. Body is read
lazily — only when a search term is supplied.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import ScribeStore


class TestSearchBreadth(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_matches_tag(self):
        self.store.add_note("todo", "nothing here", session_id="s", tags=["ticket:K-2026-99"])
        self.assertTrue(self.store.list_notes(search="K-2026-99"))

    def test_matches_body_not_in_summary(self):
        # Summary is derived from the first line; put the needle deeper in body.
        self.store.add_note("todo", "title line\n\nsecret needle in body", session_id="s")
        hits = self.store.list_notes(search="needle in body")
        self.assertEqual(len(hits), 1)

    def test_matches_session(self):
        self.store.add_note("todo", "x", session_id="abc12345")
        self.assertTrue(self.store.list_notes(search="abc12345"))

    def test_no_search_reads_no_bodies(self):
        self.store.add_note("todo", "a", session_id="s")
        self.store.add_note("todo", "b", session_id="s")
        with mock.patch.object(ScribeStore, "_read_body_any") as spy:
            self.store.list_notes()  # no search term
            spy.assert_not_called()

    def test_learning_matches_body(self):
        self.store.add_learning("frobnicate the widget", session_id="s", tags=["t1"])
        self.assertTrue(self.store.list_learnings(search="frobnicate"))

    def test_learning_matches_tag(self):
        self.store.add_learning("body text", session_id="s", tags=["alpha", "beta"])
        self.assertTrue(self.store.list_learnings(search="beta"))


if __name__ == "__main__":
    unittest.main()
