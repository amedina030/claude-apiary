"""Tests for core/utils/atomic.py — tmp+replace writes."""

import fnmatch
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.utils.atomic import write_json_atomic, write_text_atomic


class WriteTextAtomicTest(unittest.TestCase):
    def test_creates_file_and_missing_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "a" / "b" / "profile.md"
            write_text_atomic(target, "hello\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_replaces_existing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "profile.md"
            target.write_text("old", encoding="utf-8")
            write_text_atomic(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_leaves_no_temp_file_behind_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "profile.md"
            write_text_atomic(target, "x")
            self.assertEqual(sorted(p.name for p in Path(tmp).resolve().iterdir()), ["profile.md"])

    def test_failed_replace_preserves_original_and_cleans_up(self):
        # The whole point: a crash between truncate and flush must not be
        # able to destroy the previous file.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "profile.md"
            target.write_text("previous", encoding="utf-8")
            with mock.patch("core.utils.atomic.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    write_text_atomic(target, "replacement")
            self.assertEqual(target.read_text(encoding="utf-8"), "previous")
            self.assertEqual(sorted(p.name for p in Path(tmp).resolve().iterdir()), ["profile.md"])

    def test_temp_file_is_a_sibling_named_for_its_target(self):
        # budgeter.lib.logger.cleanup_session sweeps orphans with
        # glob(path.name + ".*.tmp"); the naming is a contract.
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(Path(src))
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "baseline.json"
            with mock.patch("core.utils.atomic.os.replace", side_effect=spy):
                write_text_atomic(target, "{}")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].parent, target.parent)
            self.assertTrue(
                fnmatch.fnmatch(seen[0].name, target.name + ".*.tmp"),
                f"temp name {seen[0].name!r} would escape the orphan sweep",
            )

    def test_non_utf8_encoding_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "latin.txt"
            write_text_atomic(target, "café", encoding="latin-1")
            self.assertEqual(target.read_text(encoding="latin-1"), "café")


class WriteJsonAtomicTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "config.json"
            write_json_atomic(target, {"b": 1, "a": 2})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"b": 1, "a": 2})

    def test_compact_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "config.json"
            write_json_atomic(target, {"a": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 1}')

    def test_indent_sort_keys_and_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve() / "config.json"
            write_json_atomic(
                target, {"b": 1, "a": 2}, indent=2, sort_keys=True, trailing_newline=True
            )
            text = target.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("}\n"))
            self.assertEqual(text.splitlines()[1].strip(), '"a": 2,')


if __name__ == "__main__":
    unittest.main()
