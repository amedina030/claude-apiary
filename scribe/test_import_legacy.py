"""Legacy importer + dry-run equivalence (parity spec §7).

Runs against a fully-synthetic fixture under tests/fixtures/legacy_scribe/ (no
real data). Pre-seeds the store so imported ids differ from legacy ids, making
the supersedes-rewrite assertion meaningful.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import ScribeStore
from scribe.tools import import_legacy as imp

FIXTURE = Path(__file__).resolve().parent / "tests" / "fixtures" / "legacy_scribe"


class TestImportLegacy(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))
        # Pre-seed two learnings so imported learnings get fresh, different ids
        # (L-2026-3, L-2026-4, ...) — exposes whether supersedes is rewritten.
        self.store.add_learning("preseed one", session_id="x")
        self.store.add_learning("preseed two", session_id="x")
        self.notes = imp.read_legacy_notes(FIXTURE)
        self.id_map, self.counts = imp.import_into(self.store, self.notes)

    def tearDown(self):
        self._td.cleanup()

    def test_reads_valid_notes_and_skips_corrupt_line(self):
        # 3 learnings (1 corrupt line skipped) + 3 active todos + 1 archived todo
        self.assertEqual(len(self.notes), 7)

    def test_per_type_counts(self):
        self.assertEqual(self.counts.get("learning"), 3)
        self.assertEqual(self.counts.get("todo"), 4)

    def test_tickets_skipped(self):
        self.assertNotIn("ticket", self.counts)
        self.assertFalse(any(k and k.startswith("K-") for k in self.id_map))
        self.assertFalse(any(v and v.startswith("K-") for v in self.id_map.values()))

    def test_provenance_backstamped(self):
        todos = self.store.list_notes(note_type="todo", status="all")
        origs = {n.get("orig_display_id") for n in todos}
        self.assertIn("T-2026-1", origs)
        for n in todos:
            self.assertTrue(n.get("orig_timestamp"))

    def test_supersedes_rewritten_to_new_id(self):
        new_l1 = self.id_map["L-2026-1"]
        self.assertNotEqual(new_l1, "L-2026-1")  # pre-seed shifted the ids
        learnings = self.store.list_learnings(status="all")
        beta = next(e for e in learnings if e.get("orig_display_id") == "L-2026-2")
        self.assertEqual(beta.get("supersedes"), new_l1)

    def test_archived_note_lands_in_archive(self):
        archived = self.store.list_notes(note_type="todo", status="archived")
        self.assertTrue(any(n.get("orig_display_id") == "T-2026-4" for n in archived))

    def test_dropped_status_preserved(self):
        todos = self.store.list_notes(note_type="todo", status="all")
        dropped = next(n for n in todos if n.get("orig_display_id") == "T-2026-3")
        self.assertEqual(dropped.get("status"), "dropped")

    def test_role_mission_carried_over(self):
        todos = self.store.list_notes(note_type="todo", status="all")
        rm = next(n for n in todos if n.get("orig_display_id") == "T-2026-2")
        self.assertEqual(rm.get("role"), "dev")
        self.assertEqual(rm.get("mission"), "projx")

    def test_dry_run_report_mentions_skip(self):
        report = imp.equivalence_report(
            self.notes, self.counts, imp.count_skipped(FIXTURE), self.id_map
        )
        self.assertIn("ticket", report)
        self.assertIn("skipped", report)
        self.assertIn("in=   1", report)  # one ticket skipped


if __name__ == "__main__":
    unittest.main()
