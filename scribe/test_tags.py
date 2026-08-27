"""Note tags, find_active_with_tag, and tag mutation (parity spec §5.14).

Adapted from the source oracles ``test_unique_tag`` and ``test_tag_mutation``
to apiary's ScribeStore API. The assertions are the oracle; the plumbing is
apiary's.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import ScribeStore


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()


class TestTagStorage(_Base):
    def test_tags_stored_only_when_nonempty(self):
        a = self.store.add_note("todo", "x", session_id="s", tags=["foo"])
        self.assertEqual(a["tags"], ["foo"])
        b = self.store.add_note("todo", "y", session_id="s", tags=[])
        self.assertNotIn("tags", b)
        c = self.store.add_note("todo", "z", session_id="s")
        self.assertNotIn("tags", c)


class TestFindActiveWithTag(_Base):
    def test_returns_match(self):
        a = self.store.add_note("todo", "first", session_id="s", tags=["asana:1"])
        self.store.add_note("todo", "second", session_id="s", tags=["asana:2"])
        found = self.store.find_active_with_tag("asana:1")
        self.assertIsNotNone(found)
        self.assertEqual(found["display_id"], a["display_id"])

    def test_ignores_archived(self):
        a = self.store.add_note("todo", "will-archive", session_id="s", tags=["asana:1"])
        self.store.archive_note("todo", a["year"], a["seq"])
        self.assertIsNone(self.store.find_active_with_tag("asana:1"))

    def test_ignores_done(self):
        a = self.store.add_note("todo", "done one", session_id="s", tags=["asana:1"])
        self.store.update_note("todo", a["year"], a["seq"], status="done")
        self.assertIsNone(self.store.find_active_with_tag("asana:1"))

    def test_misses_when_absent(self):
        self.store.add_note("todo", "no tags here", session_id="s")
        self.assertIsNone(self.store.find_active_with_tag("asana:999"))

    def test_works_across_types(self):
        self.store.add_note("blocker", "b", session_id="s", tags=["asana:42", "priority:high"])
        found = self.store.find_active_with_tag("priority:high")
        self.assertIsNotNone(found)
        self.assertEqual(found["type"], "blocker")


class TestTagMutation(_Base):
    def _mk(self, tags):
        return self.store.add_note("todo", "x", session_id="s", tags=tags)

    def test_add_tag(self):
        a = self._mk(["foo"])
        u = self.store.update_note("todo", a["year"], a["seq"], add_tags=["bar", "baz"])
        self.assertEqual(u["tags"], ["foo", "bar", "baz"])

    def test_add_is_idempotent(self):
        a = self._mk(["foo"])
        u = self.store.update_note("todo", a["year"], a["seq"], add_tags=["foo", "foo"])
        self.assertEqual(u["tags"], ["foo"])

    def test_remove_tag(self):
        a = self._mk(["foo", "bar", "baz"])
        u = self.store.update_note("todo", a["year"], a["seq"], remove_tags=["bar"])
        self.assertEqual(u["tags"], ["foo", "baz"])

    def test_remove_then_add_swaps_value(self):
        a = self._mk(["asana:1", "asana-status:active"])
        u = self.store.update_note(
            "todo",
            a["year"],
            a["seq"],
            remove_tags=["asana-status:active"],
            add_tags=["asana-status:done"],
        )
        self.assertNotIn("asana-status:active", u["tags"])
        self.assertIn("asana-status:done", u["tags"])
        self.assertIn("asana:1", u["tags"])

    def test_tag_mutation_works_on_done_note(self):
        a = self._mk([])
        self.store.update_note("todo", a["year"], a["seq"], status="done")
        u = self.store.update_note("todo", a["year"], a["seq"], add_tags=["asana-status:done"])
        self.assertIn("asana-status:done", u["tags"])


if __name__ == "__main__":
    unittest.main()
