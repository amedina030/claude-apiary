"""Tests for ``core/cascade.py`` — main-apiary move propagation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import cascade, drift, testing
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
    testing.init_git_repo(target)
    from core import install as install_mod

    return install_mod.install(target, apiary_repo=apiary).uid


class CascadeFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_main_apiary(self.root)
        # Bootstrap two test targets
        self.target_a = self.root / "demo_a"
        self.target_a.mkdir()
        self.uid_a = _bootstrap_target(self.target_a, self.apiary)
        self.target_b = self.root / "demo_b"
        self.target_b.mkdir()
        self.uid_b = _bootstrap_target(self.target_b, self.apiary)

    def test_cascade_rewrites_main_apiary_pointer_in_each_repo(self):
        # Pretend main-apiary moved by rewriting its own registry entry's
        # path AND running cascade against a "new" path. We use the real
        # apiary path here for simplicity — the cascade only cares that
        # the path is valid.
        new_path = self.apiary  # for this test, the path doesn't actually change
        report = cascade.cascade_fix(new_path)
        self.assertEqual(set(report.updated), {self.uid_a, self.uid_b})
        # Main-apiary's own entry is skipped (uid=1)
        self.assertNotIn(1, report.updated)

        for target in (self.target_a, self.target_b):
            mp = state.read_main_apiary_pointer(target)
            self.assertEqual(Path(mp["main_apiary_path"]).resolve(), new_path.resolve())

    def test_cascade_skips_repos_whose_real_path_is_gone(self):
        # Move (rather than delete) target_b — Windows .git/objects can
        # be read-only and refuse rmtree, but renaming is always safe.
        moved_aside = self.root / "demo_b_moved"
        self.target_b.rename(moved_aside)
        try:
            report = cascade.cascade_fix(self.apiary)
            self.assertIn(self.uid_a, report.updated)
            skipped_uids = [u for u, _ in report.skipped]
            self.assertIn(self.uid_b, skipped_uids)
        finally:
            # Restore so tmpdir cleanup can run normally.
            moved_aside.rename(self.target_b)


class MainApiaryDriftDispatchTests(unittest.TestCase):
    """Drift handler should run cascade-fix when main-apiary itself moves
    (uid=1 is the dispatch signal)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary = _make_main_apiary(self.root)
        self.target = self.root / "demo"
        self.target.mkdir()
        self.uid = _bootstrap_target(self.target, self.apiary)

    def test_main_apiary_drift_triggers_cascade(self):
        # Pretend main-apiary moved by rewriting its self-pointer's
        # recorded path. The drift handler should detect drift, update
        # main-apiary's own pointers, and cascade-fix the bootstrapped repo.
        sp = state.read_self_pointer(self.apiary)
        sp["real_path"] = str(self.root / "old-main-apiary-location")
        state.write_self_pointer(self.apiary, sp)

        report = drift.check_and_handle(self.apiary)
        self.assertEqual(report.action, "move")
        self.assertEqual(report.old_uid, 1)

        # Main-apiary's own self-pointer now matches actual location.
        sp_after = state.read_self_pointer(self.apiary)
        self.assertEqual(Path(sp_after["real_path"]).resolve(), self.apiary.resolve())

        # Bootstrapped repo's main-apiary-pointer was updated.
        mp = state.read_main_apiary_pointer(self.target)
        self.assertEqual(Path(mp["main_apiary_path"]).resolve(), self.apiary.resolve())


if __name__ == "__main__":
    unittest.main()
