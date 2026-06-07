"""Tests for gui.file_refs — the per-run drag-drop reference registry.

All tests run against a TemporaryDirectory, never the real GUI state dir, and
reference real temp files (the registry stores pointers, not copies).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gui.file_refs import FileRefs


class FileRefsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.refs = FileRefs(store=self.tmp / "file_refs.json")
        self.refs.reset()

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, name: str, body: bytes = b"x") -> Path:
        p = self.tmp / name
        p.write_bytes(body)
        return p

    def test_add_records_reference_without_copying(self):
        src = self._make("shot.png", b"\x89PNG")
        desc = self.refs.add(str(src))
        self.assertTrue(desc["ok"])
        self.assertEqual(desc["name"], "shot.png")
        self.assertEqual(desc["type"], "image/png")
        self.assertEqual(desc["path"], str(src.resolve()))
        self.assertEqual(desc["size"], 4)
        self.assertIn("added", desc)
        # The original is the only copy — nothing was duplicated anywhere.
        self.assertEqual(src.read_bytes(), b"\x89PNG")

    def test_add_rejects_missing_file(self):
        desc = self.refs.add(str(self.tmp / "nope.txt"))
        self.assertFalse(desc["ok"])
        self.assertIn("not a file", desc["error"])

    def test_add_rejects_blank(self):
        self.assertFalse(self.refs.add("")["ok"])

    def test_add_is_idempotent_on_same_path(self):
        src = self._make("a.txt")
        a = self.refs.add(str(src))
        b = self.refs.add(str(src))
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.refs.list()), 1)

    def test_persisted_as_json_list_with_location_and_timestamp(self):
        src = self._make("a.txt")
        self.refs.add(str(src))
        import json
        data = json.loads((self.tmp / "file_refs.json").read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["path"], str(src.resolve()))
        self.assertIn("added", data[0])

    def test_remove_drops_reference_but_keeps_file(self):
        src = self._make("a.txt")
        desc = self.refs.add(str(src))
        self.assertTrue(self.refs.remove(desc["id"]))
        self.assertEqual(self.refs.list(), [])
        self.assertTrue(src.is_file())  # file itself untouched

    def test_remove_unknown_id(self):
        self.assertFalse(self.refs.remove("deadbeef"))
        self.assertFalse(self.refs.remove(""))

    def test_clear(self):
        self.refs.add(str(self._make("a.txt")))
        self.refs.add(str(self._make("b.txt")))
        self.assertEqual(len(self.refs.list()), 2)
        self.assertTrue(self.refs.clear())
        self.assertEqual(self.refs.list(), [])

    def test_reset_wipes_leftovers(self):
        self.refs.add(str(self._make("a.txt")))
        self.refs.reset()
        self.assertEqual(self.refs.list(), [])

    def test_list_flags_missing_target(self):
        src = self._make("gone.txt")
        self.refs.add(str(src))
        self.assertTrue(self.refs.list()[0]["exists"])
        src.unlink()  # user moved/deleted the original after referencing it
        entry = self.refs.list()[0]
        self.assertFalse(entry["exists"])
        # The reference is retained (the path tradeoff is surfaced, not hidden).
        self.assertEqual(entry["name"], "gone.txt")

    def test_corrupt_store_recovers_to_empty(self):
        (self.tmp / "file_refs.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.refs.list(), [])
        # And a subsequent add still works (overwrites the garbage).
        self.assertTrue(self.refs.add(str(self._make("a.txt")))["ok"])

    # --- manifest_and_mark (authoritative send-time path, option B) ----------

    def test_add_starts_unannounced(self):
        desc = self.refs.add(str(self._make("a.txt")))
        self.assertFalse(desc["announced"])

    def test_manifest_lists_fresh_then_marks_announced(self):
        src = self._make("shot.png", b"\x89PNG")
        self.refs.add(str(src))
        out = self.refs.manifest_and_mark()
        # Fresh file is listed with path + mime, under the Read-tool header.
        self.assertIn("read these with the Read tool", out["text"])
        self.assertIn(f"- {src.resolve()} (image/png)", out["text"])
        self.assertTrue(out["files"][0]["announced"])
        # Second send doesn't re-list it — it's in the conversation history now.
        again = self.refs.manifest_and_mark()
        self.assertEqual(again["text"], "")

    def test_manifest_skips_missing_target_and_does_not_announce_it(self):
        src = self._make("gone.txt")
        self.refs.add(str(src))
        src.unlink()  # vanished between drop and send — the bug this fixes
        out = self.refs.manifest_and_mark()
        # Dead path is never shipped...
        self.assertEqual(out["text"], "")
        # ...the reference is retained and flagged missing for the UI...
        self.assertFalse(out["files"][0]["exists"])
        # ...and it stays unannounced, so if the file returns it'll be sent.
        self.assertFalse(out["files"][0]["announced"])

    def test_manifest_returns_only_fresh_when_one_already_shared(self):
        a = self._make("a.txt")
        self.refs.add(str(a))
        self.refs.manifest_and_mark()  # a is now announced
        b = self._make("b.txt")
        self.refs.add(str(b))
        out = self.refs.manifest_and_mark()
        self.assertNotIn(str(a.resolve()), out["text"])
        self.assertIn(str(b.resolve()), out["text"])

    def test_manifest_empty_with_no_refs(self):
        out = self.refs.manifest_and_mark()
        self.assertEqual(out["text"], "")
        self.assertEqual(out["files"], [])


if __name__ == "__main__":
    unittest.main()
