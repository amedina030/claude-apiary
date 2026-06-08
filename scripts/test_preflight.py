"""Tests for ``scripts/preflight.py`` — pure decision helpers.

The check_* wrappers read the live environment (interpreter version, PATH,
home dir), so the testable logic is factored into pure helpers; these lock in
the version-pin and fragile-path rules without touching real user data.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import preflight


class GuiPythonStatusTests(unittest.TestCase):
    def test_312_is_ok(self) -> None:
        self.assertEqual(preflight._gui_python_status((3, 12)), preflight.OK)

    def test_311_is_ok(self) -> None:
        self.assertEqual(preflight._gui_python_status((3, 11)), preflight.OK)

    def test_313_warns(self) -> None:
        # Installs anyway but warns loudly — pythonnet has no 3.13 wheel.
        self.assertEqual(preflight._gui_python_status((3, 13)), preflight.WARN)

    def test_314_warns(self) -> None:
        self.assertEqual(preflight._gui_python_status((3, 14)), preflight.WARN)

    def test_310_fails(self) -> None:
        self.assertEqual(preflight._gui_python_status((3, 10)), preflight.FAIL)


class PathFlagTests(unittest.TestCase):
    def test_clean_path_has_no_flags(self) -> None:
        self.assertEqual(preflight._path_flags("/home/amedi", "/opt/claude-apiary/scripts/x.py"), [])

    def test_apostrophe_is_flagged(self) -> None:
        flags = preflight._path_flags(r"C:\Users\Nelson's PC", r"D:\repo\x.py")
        self.assertEqual(len(flags), 1)
        self.assertIn("apostrophe", flags[0])

    def test_space_is_flagged(self) -> None:
        flags = preflight._path_flags(r"C:\Users\John Doe", r"D:\repo\x.py")
        self.assertIn("space", flags[0])

    def test_non_ascii_is_flagged(self) -> None:
        flags = preflight._path_flags("/home/josé", "/opt/repo/x.py")
        self.assertIn("non-ASCII", flags[0])

    def test_both_paths_flagged(self) -> None:
        flags = preflight._path_flags(r"C:\Users\Nelson's PC", r"C:\Users\Nelson's PC\repo\x.py")
        self.assertEqual(len(flags), 2)


class ShimDetectionTests(unittest.TestCase):
    def test_cmd_is_a_shim(self) -> None:
        self.assertTrue(preflight._is_shim(r"C:\npm\claude.cmd"))

    def test_exe_is_not_a_shim(self) -> None:
        self.assertFalse(preflight._is_shim(r"C:\Program Files\claude\claude.exe"))

    def test_unix_binary_is_not_a_shim(self) -> None:
        self.assertFalse(preflight._is_shim("/usr/local/bin/claude"))


if __name__ == "__main__":
    unittest.main()
