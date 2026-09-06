"""Tests for core/utils/longpath.py (T-2026-303)."""

import os
import tempfile
import unittest
from pathlib import Path

from core.utils import longpath


class RmtreeLongTests(unittest.TestCase):
    def test_removes_a_normal_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wt"
            (root / "a" / "b").mkdir(parents=True)
            (root / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
            longpath.rmtree_long(root)
            self.assertFalse(root.exists())

    def test_ignore_errors_on_missing_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            longpath.rmtree_long(Path(tmp) / "absent", ignore_errors=True)  # must not raise
            with self.assertRaises(OSError):
                longpath.rmtree_long(Path(tmp) / "absent")


@unittest.skipUnless(os.name == "nt", "extended-length prefix is Windows-only")
class WindowsExtendedPathTests(unittest.TestCase):
    def test_prefix_added_once_and_path_made_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x"
            ext = longpath.extended_path(p)
            self.assertTrue(ext.startswith(longpath.EXTENDED_PREFIX))
            self.assertTrue(ext.endswith("\\x"))
            self.assertEqual(longpath.extended_path(ext), ext)

    def test_tree_deeper_than_max_path_is_removed(self):
        # The failure T-2026-303 tracks: a worktree with a venv in it holds
        # paths past 260 characters, which plain deletion cannot reach.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wt"
            deep = root
            while len(str(deep)) < 300:
                deep = deep / ("segment_" + "x" * 40)
            os.makedirs(longpath.extended_path(deep))
            with open(longpath.extended_path(deep / "leaf.txt"), "w", encoding="utf-8") as fh:
                fh.write("x")
            self.assertGreater(len(str(deep)), 260)
            longpath.rmtree_long(root)
            self.assertFalse(root.exists())


@unittest.skipIf(os.name == "nt", "pass-through behaviour is for POSIX")
class PosixPassThroughTests(unittest.TestCase):
    def test_path_is_returned_unchanged(self):
        self.assertEqual(longpath.extended_path("/tmp/relative/../x"), "/tmp/relative/../x")
        self.assertEqual(longpath.extended_path(Path("rel/dir")), "rel/dir")


if __name__ == "__main__":
    unittest.main()
