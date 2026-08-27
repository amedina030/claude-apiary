"""Tolerant mark-done cascade (parity spec §5.14 / assumption A2).

Apiary holds no local canonical (``K``) tickets, so marking a ticket-linked
todo done is a no-op close *signal* — it must never raise and never close a
local note.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe import notes as notes_mod
from scribe import policy
from scribe.store import ScribeStore


class TestCascade(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _done(self, display_id):
        notes_mod.cmd_done(SimpleNamespace(store=self.store, id=display_id))

    def test_external_ticket_links_returns_only_k_prefixed(self):
        note = {"tags": ["ticket:K-2026-99", "ticket:T-2026-5", "priority:high", "ticket:K-2026-7"]}
        self.assertEqual(policy.external_ticket_links(note), ["K-2026-99", "K-2026-7"])

    def test_external_links_empty_when_no_ticket_tags(self):
        self.assertEqual(policy.external_ticket_links({"tags": ["priority:high"]}), [])
        self.assertEqual(policy.external_ticket_links({}), [])

    def test_done_on_external_ticket_tagged_todo_succeeds(self):
        a = self.store.add_note("todo", "mirror", session_id="s", tags=["ticket:K-2026-99"])
        self._done(a["display_id"])  # must not raise
        fetched = self.store.get_note("todo", a["year"], a["seq"])
        self.assertEqual(fetched["status"], "done")

    def test_done_never_cascades_into_a_local_note(self):
        target = self.store.add_note("todo", "local target", session_id="s")
        linker = self.store.add_note(
            "todo", "linker", session_id="s", tags=[f"ticket:{target['display_id']}"]
        )
        self._done(linker["display_id"])
        after = self.store.get_note("todo", target["year"], target["seq"])
        self.assertEqual(after["status"], "active")  # untouched — no local cascade


if __name__ == "__main__":
    unittest.main()
