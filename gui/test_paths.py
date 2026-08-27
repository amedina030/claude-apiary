"""Tests for GUI state-directory resolution (source vs frozen builds)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from gui import paths


def _make_checkout(root: Path) -> Path:
    """Create a fake apiary checkout (``.git`` + ``gui/``) under ``root``."""
    (root / ".git").mkdir()
    (root / "gui").mkdir()
    return root


class StateDirSourceTests(unittest.TestCase):
    """Non-frozen (source) build behaviour — unchanged grandparent resolution."""

    def test_source_resolves_to_checkout_root(self):
        # gui/paths.py lives at <checkout>/gui/paths.py, so main-apiary is its
        # grandparent and state hangs off <checkout>/.apiary/gui/.
        expected = Path(paths.__file__).resolve().parent.parent
        self.assertEqual(paths.state_dir(), expected / ".apiary" / "gui" / "apiary_gui")

    def test_profile_reroots_state(self):
        with mock.patch.dict("os.environ", {"APIARY_GUI_PROFILE": "dev"}):
            self.assertTrue(str(paths.state_dir()).endswith("apiary_gui_dev"))


class StateDirFrozenTests(unittest.TestCase):
    """Frozen (PyInstaller) build behaviour — the bug this fix targets."""

    def test_frozen_anchors_to_checkout_not_bundle(self):
        # exe sits at <checkout>/dist/apiary-gui/apiary-gui.exe; state must
        # resolve to <checkout>/.apiary/gui, NOT inside the dist bundle.
        with TemporaryDirectory() as tmp:
            checkout = _make_checkout(Path(tmp))
            exe = checkout / "dist" / "apiary-gui" / "apiary-gui.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            with (
                mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(sys, "executable", str(exe)),
            ):
                self.assertEqual(
                    paths.state_dir(),
                    checkout / ".apiary" / "gui" / "apiary_gui",
                )

    def test_frozen_falls_back_to_user_data_when_no_checkout(self):
        # Build shipped outside any checkout: no .git ancestor → user data dir.
        with TemporaryDirectory() as tmp, TemporaryDirectory() as data:
            exe = Path(tmp) / "somewhere" / "apiary-gui.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            data_base = Path(data)
            with (
                mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(sys, "executable", str(exe)),
                mock.patch.object(paths, "_user_data_base", return_value=data_base),
            ):
                self.assertEqual(
                    paths.state_dir(),
                    data_base / ".apiary" / "gui" / "apiary_gui",
                )


class FindCheckoutTests(unittest.TestCase):
    def test_requires_both_git_and_gui(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()  # .git but no gui/ → not a checkout
            start = root / "dist" / "apiary-gui"
            start.mkdir(parents=True)
            self.assertIsNone(paths._find_apiary_checkout(start))

    def test_finds_nearest_checkout_ancestor(self):
        with TemporaryDirectory() as tmp:
            checkout = _make_checkout(Path(tmp))
            start = checkout / "dist" / "apiary-gui"
            start.mkdir(parents=True)
            self.assertEqual(paths._find_apiary_checkout(start), checkout)


if __name__ == "__main__":
    unittest.main()
