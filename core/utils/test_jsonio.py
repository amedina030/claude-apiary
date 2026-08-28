"""Tests for core/utils/jsonio.py — the one tolerant JSON-object reader."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.utils.jsonio import read_json_object


class ReadJsonObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def _write(self, name: str, text: str) -> Path:
        p = self.root / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_an_object(self) -> None:
        p = self._write("a.json", '{"uid": 1, "name": "x"}')
        self.assertEqual(read_json_object(p), {"uid": 1, "name": "x"})

    def test_accepts_a_string_path(self) -> None:
        p = self._write("a.json", '{"k": true}')
        self.assertEqual(read_json_object(str(p)), {"k": True})

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(read_json_object(self.root / "nope.json"))

    def test_directory_returns_none(self) -> None:
        d = self.root / "adir"
        d.mkdir()
        self.assertIsNone(read_json_object(d))

    def test_malformed_json_returns_none(self) -> None:
        p = self._write("bad.json", '{"uid": 1,')
        self.assertIsNone(read_json_object(p))

    def test_empty_file_returns_none(self) -> None:
        self.assertIsNone(read_json_object(self._write("empty.json", "")))

    def test_non_object_top_level_returns_none(self) -> None:
        # A truncated writer can leave a bare list or scalar; callers all
        # want a dict, so this is "no usable object", not a surprise type.
        self.assertIsNone(read_json_object(self._write("list.json", "[1, 2]")))
        self.assertIsNone(read_json_object(self._write("num.json", "42")))
        self.assertIsNone(read_json_object(self._write("null.json", "null")))

    def test_binary_garbage_returns_none(self) -> None:
        p = self.root / "bin.json"
        p.write_bytes(b"\xff\xfe\x00\x01not utf-8")
        self.assertIsNone(read_json_object(p))

    def test_empty_object_is_returned_not_conflated_with_missing(self) -> None:
        # `read_json_object(p) or {}` is the documented idiom for callers
        # that treat absent as empty; the raw return distinguishes them.
        p = self._write("empty-obj.json", "{}")
        self.assertEqual(read_json_object(p), {})
        self.assertIsNotNone(read_json_object(p))


if __name__ == "__main__":
    unittest.main()
