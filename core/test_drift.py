"""Tests for ``core/drift.py`` — per-repo drift detection."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import drift, testing
from core.utils import state


def _make_main_apiary(root: Path) -> Path:
    """A fake main-apiary that has been self-bootstrapped, at *root*/main-apiary.

    Drift refuses to act unless main-apiary is a git repo carrying its own
    self-pointer, so both are on (see core/drift.py::_verify_main_apiary).
    """
    return testing.make_fake_apiary(
        root,
        name="main-apiary",
        git=True,
        self_bootstrap=True,
    )


def _bootstrap_target(target: Path, apiary: Path) -> int:
    """Install apiary into *target* and return its uid."""
    testing.init_git_repo(target)
    from core import install as install_mod

    return install_mod.install(target, apiary_repo=apiary).uid


class CheckAndHandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_main_apiary(self.root)
        self.target = self.root / "demo"
        self.target.mkdir()
        self.uid = _bootstrap_target(self.target, self.apiary)

    def test_no_drift_returns_none_and_refreshes_check_ts(self):
        # Force a different timestamp by resetting it
        sp = state.read_self_pointer(self.target)
        sp["last_drift_check"] = "2000-01-01T00:00:00Z"
        state.write_self_pointer(self.target, sp)

        report = drift.check_and_handle(self.target)
        self.assertEqual(report.action, "none")
        after = state.read_self_pointer(self.target)["last_drift_check"]
        self.assertNotEqual(after, "2000-01-01T00:00:00Z")

    def test_unbootstrapped_target_returns_not_bootstrapped(self):
        plain = self.root / "plain"
        plain.mkdir()
        report = drift.check_and_handle(plain)
        self.assertEqual(report.action, "not_bootstrapped")

    def test_missing_main_apiary_returns_skip(self):
        # Point the repo's main-apiary-pointer at a nonexistent dir.
        state.write_main_apiary_pointer(
            self.target,
            {
                "main_apiary_path": str(self.root / "nope"),
                "main_apiary_uid": 1,
                "registered_at": "2026-05-05T00:00:00Z",
            },
        )
        report = drift.check_and_handle(self.target)
        self.assertEqual(report.action, "skip")
        self.assertIn("main checkout not found", report.message)

    def test_main_apiary_self_pointer_drift_returns_skip(self):
        # Main-apiary itself appears moved.
        main_self = state.read_self_pointer(self.apiary)
        main_self["real_path"] = str(self.root / "moved-main")
        state.write_self_pointer(self.apiary, main_self)
        report = drift.check_and_handle(self.target)
        self.assertEqual(report.action, "skip")
        self.assertIn("self-pointer out of sync", report.message)

    def test_move_scenario_updates_the_registry_inline(self):
        # Pretend the repo moved by overwriting its self-pointer's recorded path.
        sp = state.read_self_pointer(self.target)
        sp["real_path"] = str(self.root / "old-location")
        state.write_self_pointer(self.target, sp)
        # ... and by pointing the registry entry at the same stale path, so
        # the assertion below can only pass if the handler rewrote it.
        registry = state._load_registry(self.apiary)
        registry[str(self.uid)]["real_path"] = str(self.root / "old-location")
        state._save_registry(self.apiary, registry)

        report = drift.check_and_handle(self.target)
        self.assertEqual(report.action, "move")
        self.assertEqual(report.old_uid, self.uid)
        self.assertEqual(report.new_uid, self.uid)

        # Registry updated in place — no second consumer needed.
        entry = state._load_registry(self.apiary)[str(self.uid)]
        self.assertEqual(Path(entry["real_path"]).resolve(), self.target.resolve())
        self.assertIn("last_used", entry)

        # Self-pointer real_path now matches actual.
        sp_after = state.read_self_pointer(self.target)
        self.assertEqual(Path(sp_after["real_path"]).resolve(), self.target.resolve())

    def test_move_with_no_registry_entry_reports_the_repair_step(self):
        # Registry loss (e.g. a partially failed uninstall) leaves a pin file
        # with no entry. The handler must not invent one, and must say so.
        registry = state._load_registry(self.apiary)
        registry.pop(str(self.uid), None)
        state._save_registry(self.apiary, registry)
        sp = state.read_self_pointer(self.target)
        sp["real_path"] = str(self.root / "old-location")
        state.write_self_pointer(self.target, sp)

        report = drift.check_and_handle(self.target)
        self.assertEqual(report.action, "move")
        self.assertIn("no entry for uid", report.message)
        self.assertNotIn(str(self.uid), state._load_registry(self.apiary))
        # The self-pointer is still repaired so the launcher stays consistent.
        sp_after = state.read_self_pointer(self.target)
        self.assertEqual(Path(sp_after["real_path"]).resolve(), self.target.resolve())

    def test_copy_scenario_allocates_new_uid_and_registers_it(self):
        # Copy the bootstrapped target to a new path. Since the original is
        # still on disk with the same uid, we should be classified as copy.
        copy_target = self.root / "demo-copy"
        shutil.copytree(self.target, copy_target)

        report = drift.check_and_handle(copy_target)
        self.assertEqual(report.action, "copy")
        self.assertEqual(report.old_uid, self.uid)
        self.assertGreater(report.new_uid, self.uid)

        # The copy now has a new uid in its own self-pointer.
        copy_sp = state.read_self_pointer(copy_target)
        self.assertEqual(copy_sp["uid"], report.new_uid)

        # ... and a registry entry of its own, written under the same lock.
        registry = state._load_registry(self.apiary)
        entry = registry[str(report.new_uid)]
        self.assertEqual(entry["uid"], report.new_uid)
        self.assertEqual(Path(entry["real_path"]).resolve(), copy_target.resolve())
        # The original's entry is untouched.
        self.assertEqual(
            Path(registry[str(self.uid)]["real_path"]).resolve(),
            self.target.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
