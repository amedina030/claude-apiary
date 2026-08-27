"""Atomic index-write guarantees (parity spec §5.1).

A kill mid-write must never corrupt index.jsonl: the file is written to a temp
sibling then os.replace'd — by ``core.utils.atomic.write_text_atomic``, the one
copy of that pattern — so an interrupted write leaves the prior file intact and
no torn line.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import ScribeStore


class TestAtomicIndex(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = ScribeStore(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def _year_dir(self, entry):
        return self.store.state_dir / "todos" / str(entry["year"])

    def test_normal_writes_leave_no_temp_files(self):
        a = self.store.add_note("todo", "one", session_id="s")
        self.store.add_note("todo", "two", session_id="s")
        self.store.update_note("todo", a["year"], a["seq"], status="done")
        leftovers = [p.name for p in self._year_dir(a).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_failed_replace_preserves_original_and_cleans_temp(self):
        a = self.store.add_note("todo", "original", session_id="s")
        year_dir = self._year_dir(a)
        idx = year_dir / "index.jsonl"
        before = idx.read_text(encoding="utf-8")
        with mock.patch("core.utils.atomic.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                ScribeStore._write_index(year_dir, [{"seq": 99, "display_id": "T-2026-99"}])
        self.assertEqual(idx.read_text(encoding="utf-8"), before)
        leftovers = [p.name for p in year_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_append_preserves_all_lines(self):
        entries = [self.store.add_note("todo", f"n{i}", session_id="s") for i in range(5)]
        rows = self.store._read_index(self._year_dir(entries[0]))
        self.assertEqual(sorted(e["seq"] for e in rows), sorted(e["seq"] for e in entries))


if __name__ == "__main__":
    unittest.main()
