"""Tests for gui/picker.py — directory listing for the themed folder picker."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from gui import picker


class ListDirectoryTests(unittest.TestCase):
    def test_lists_subdirectories_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            (root / "file.txt").write_text("x", encoding="utf-8")
            result = picker.list_directory(str(root))
            names = [e["name"] for e in result["entries"]]
            self.assertEqual(names, ["alpha", "beta"])
            self.assertIsNone(result["error"])
            self.assertFalse(result["is_root"])

    def test_sort_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "Banana").mkdir()
            (root / "apple").mkdir()
            (root / "Cherry").mkdir()
            result = picker.list_directory(str(root))
            names = [e["name"] for e in result["entries"]]
            self.assertEqual(names, ["apple", "Banana", "Cherry"])

    def test_missing_path_returns_error(self) -> None:
        result = picker.list_directory(str(Path(tempfile.gettempdir()) / "_nope_apiary_picker"))
        self.assertEqual(result["entries"], [])
        self.assertIsNotNone(result["error"])

    def test_file_path_returns_error(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            p = Path(tf.name)
        try:
            result = picker.list_directory(str(p))
            self.assertIn("not a directory", result["error"])
        finally:
            p.unlink(missing_ok=True)

    def test_empty_path_returns_root_view(self) -> None:
        result = picker.list_directory(None)
        self.assertTrue(result["is_root"] or result["path"] == str(Path.home()))

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows-specific")
    def test_windows_root_returns_drives(self) -> None:
        result = picker.list_directory("")
        self.assertTrue(result["is_root"])
        self.assertIsNone(result["parent"])
        self.assertTrue(any(e["name"].endswith(":\\") for e in result["entries"]))

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows-specific")
    def test_windows_drive_root_parent_is_computer(self) -> None:
        result = picker.list_directory("C:\\")
        self.assertEqual(result["parent"], "")


class PickerContextTests(unittest.TestCase):
    def test_returns_recents_home_initial(self) -> None:
        ctx = picker.picker_context()
        self.assertIn("recents", ctx)
        self.assertIn("home", ctx)
        self.assertIn("initial", ctx)
        self.assertIsInstance(ctx["recents"], list)
        self.assertEqual(ctx["home"], str(Path.home()))


if __name__ == "__main__":
    unittest.main()
