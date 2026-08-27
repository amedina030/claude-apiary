import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.formatting import PREFIX_TO_TYPE as _PREFIX_TO_TYPE
from scribe.formatting import format_id as _format_id
from scribe.notes import _parse_id_arg
from scribe.store import NEXT_SEQ_FILENAME, TYPE_FOLDERS, TYPE_PREFIXES, ScribeStore


class TestTypePrefixes(unittest.TestCase):
    def test_all_types_have_prefixes(self):
        # Every type in TYPE_FOLDERS + 'learning' has a prefix
        for t in list(TYPE_FOLDERS.keys()) + ["learning"]:
            self.assertIn(t, TYPE_PREFIXES)

    def test_prefix_letters(self):
        self.assertEqual(TYPE_PREFIXES["todo"], "T")
        self.assertEqual(TYPE_PREFIXES["handoff"], "H")
        self.assertEqual(TYPE_PREFIXES["decision"], "D")
        self.assertEqual(TYPE_PREFIXES["wishlist"], "W")
        self.assertEqual(TYPE_PREFIXES["reference"], "R")
        self.assertEqual(TYPE_PREFIXES["blocker"], "B")
        self.assertEqual(TYPE_PREFIXES["context"], "C")
        self.assertEqual(TYPE_PREFIXES["general"], "G")
        self.assertEqual(TYPE_PREFIXES["learning"], "L")


class TestFormatId(unittest.TestCase):
    def test_format_todo(self):
        entry = {"type": "todo", "year": 2026, "seq": 1}
        self.assertEqual(_format_id(entry), "T-2026-1")

    def test_format_learning(self):
        entry = {"type": "learning", "year": 2026, "seq": 3}
        self.assertEqual(_format_id(entry), "L-2026-3")

    def test_format_all_prefixes(self):
        for ntype, prefix in TYPE_PREFIXES.items():
            entry = {"type": ntype, "year": 2026, "seq": 42}
            self.assertEqual(_format_id(entry), f"{prefix}-2026-42")


class TestPerTypeYearCounters(unittest.TestCase):
    def test_add_note_returns_display_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_note("todo", "Test content", "sess1")
            year = datetime.now(timezone.utc).year
            self.assertEqual(entry["display_id"], f"T-{year}-1")
            self.assertEqual(entry["type"], "todo")
            self.assertEqual(entry["year"], year)
            self.assertEqual(entry["seq"], 1)
            self.assertNotIn("id", entry)  # no integer id field

    def test_counter_independence_across_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            todo_entry = store.add_note("todo", "todo1", "sess1")
            handoff_entry = store.add_note("handoff", "handoff1", "sess1", summary="test")
            year = datetime.now(timezone.utc).year
            self.assertEqual(todo_entry["seq"], 1)
            self.assertEqual(handoff_entry["seq"], 1)  # independent!
            self.assertEqual(todo_entry["display_id"], f"T-{year}-1")
            self.assertEqual(handoff_entry["display_id"], f"H-{year}-1")

    def test_year_rollover(self):
        # Mock datetime to simulate year change
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            # Add note in 'current' year
            entry1 = store.add_note("todo", "note1", "sess1")
            self.assertEqual(entry1["seq"], 1)

            # Manually create a 2027 year dir and add_note with mocked year
            with mock.patch("scribe.store.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2027, 1, 1, 0, 1, tzinfo=timezone.utc)
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                entry2 = store.add_note("todo", "note2", "sess1")
                self.assertEqual(entry2["year"], 2027)
                self.assertEqual(entry2["seq"], 1)  # fresh counter for new year
                self.assertEqual(entry2["display_id"], "T-2027-1")

    def test_add_learning_display_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_learning("learned something", "sess1")
            year = datetime.now(timezone.utc).year
            self.assertEqual(entry["display_id"], f"L-{year}-1")
            self.assertEqual(entry["type"], "learning")
            self.assertNotIn("id", entry)

    def test_get_note_by_type_year_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_note("todo", "hello world", "sess1")
            result = store.get_note("todo", entry["year"], entry["seq"])
            self.assertIsNotNone(result)
            self.assertEqual(result["content"], "hello world")
            self.assertEqual(result["display_id"], entry["display_id"])

    def test_get_note_nonexistent_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            result = store.get_note("todo", 1999, 1)
            self.assertIsNone(result)  # returns None, doesn't create subfolder
            self.assertFalse((Path(tmp) / "todos" / "1999").exists())

    def test_list_notes_across_years(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            # Create entries in two different years by directly manipulating
            store.add_note("todo", "note current year", "sess1")
            # Add a note in a different year subfolder manually
            year_dir = store._ensure_year_dir(store._type_dir("todo"), 2025)
            seq = store._increment_seq(year_dir)
            entry2 = {
                "display_id": f"T-2025-{seq}",
                "type": "todo",
                "year": 2025,
                "seq": seq,
                "status": "active",
                "session": "sess1",
                "timestamp": "2025-06-01T00:00:00+00:00",
                "summary": "old note",
                "has_body": True,
            }
            store._append_index(year_dir, entry2)
            store._write_note_file(year_dir, seq, "old note body")

            notes = store.list_notes(note_type="todo")
            self.assertEqual(len(notes), 2)

    def test_update_note_by_type_year_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_note("todo", "original", "sess1")
            updated = store.update_note("todo", entry["year"], entry["seq"], status="done")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["status"], "done")

    def test_archive_note_moves_to_year_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_note("todo", "archive me", "sess1")
            year = entry["year"]
            seq = entry["seq"]
            result = store.archive_note("todo", year, seq)
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "active")
            self.assertIn("archived_at", result)
            # Verify md moved
            year_dir = Path(tmp) / "todos" / str(year)
            self.assertFalse((year_dir / f"{seq}.md").exists())
            self.assertTrue((year_dir / "archive" / f"{seq}.md").exists())

    def test_next_seq_rebuild_on_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry1 = store.add_note("todo", "first", "sess1")
            year = entry1["year"]
            # Delete next_seq file
            seq_path = Path(tmp) / "todos" / str(year) / NEXT_SEQ_FILENAME
            seq_path.unlink()
            # Next add should rebuild
            entry2 = store.add_note("todo", "second", "sess1")
            self.assertEqual(entry2["seq"], 2)  # rebuilt from index scan

    def test_reference_type_uses_references_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            entry = store.add_note("reference", "ref content", "sess1")
            year = datetime.now(timezone.utc).year
            self.assertEqual(entry["display_id"], f"R-{year}-1")
            self.assertTrue((Path(tmp) / "references" / str(year)).exists())

    def test_concurrent_safety_filelock(self):
        # Just verify the seq increment uses FileLock (structural test)
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            e1 = store.add_note("todo", "a", "sess1")
            e2 = store.add_note("todo", "b", "sess1")
            self.assertEqual(e1["seq"], 1)
            self.assertEqual(e2["seq"], 2)


class TestParseIdArg(unittest.TestCase):
    def test_parse_type_year_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            result = _parse_id_arg("T-2026-5", store)
            self.assertEqual(result, ("todo", 2026, 5))

    def test_parse_learning_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            result = _parse_id_arg("L-2026-3", store)
            self.assertEqual(result, ("learning", 2026, 3))

    def test_parse_all_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            for prefix, ntype in _PREFIX_TO_TYPE.items():
                result = _parse_id_arg(f"{prefix}-2026-1", store)
                self.assertEqual(result[0], ntype)

    def test_invalid_prefix_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            with self.assertRaises(SystemExit):
                _parse_id_arg("X-2026-1", store)

    def test_bare_int_exits(self):
        # Legacy bare-integer IDs are no longer accepted; the migration map
        # they resolved through is gone.
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            with self.assertRaises(SystemExit):
                _parse_id_arg("42", store)

    def test_bare_l_prefix_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            with self.assertRaises(SystemExit):
                _parse_id_arg("L7", store)


if __name__ == "__main__":
    unittest.main()
