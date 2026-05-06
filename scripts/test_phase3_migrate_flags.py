"""Tests for ``scripts/phase3_migrate_flags.py`` — flag-file migration."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import phase3_migrate_flags as mig
from core.utils import state


def _make_apiary_with_repos(root: Path, *, repo_count: int = 2) -> tuple[Path, list[Path]]:
    """Build a fake main-apiary with N bootstrapped repos. Returns
    (apiary_path, [repo_paths]). Each repo has a .claude/apiary/ dir so
    the flags-dir target exists."""
    apiary = root / "apiary"
    apiary.mkdir()
    (apiary / ".repos").mkdir()
    repos = []
    registry = {}
    for i in range(1, repo_count + 1):
        repo = root / f"repo{i}"
        repo.mkdir()
        (repo / ".claude" / "apiary").mkdir(parents=True)
        repos.append(repo)
        registry[str(i)] = {
            "name": f"repo{i}",
            "real_path": str(repo),
            "uid": i,
            "version": "0.1.0",
        }
    (apiary / ".repos" / "registry.json").write_text(
        json.dumps(registry, indent=2), encoding="utf-8",
    )
    return apiary, repos


class PlanCopiesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.apiary, self.repos = _make_apiary_with_repos(self.root, repo_count=2)
        self.global_dir = self.root / "global"
        self.global_dir.mkdir()

    def test_no_global_flags_yields_empty_plan(self):
        plan = mig.plan_copies(self.apiary, global_dir=self.global_dir)
        self.assertEqual(plan, [])

    def test_single_global_flag_propagates_to_every_repo(self):
        (self.global_dir / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        plan = mig.plan_copies(self.apiary, global_dir=self.global_dir)
        # Two repos × one flag = two planned copies
        self.assertEqual(len(plan), 2)
        flags = {entry[2] for entry in plan}
        self.assertEqual(flags, {"budgeter-log"})

    def test_existing_per_repo_flag_is_skipped(self):
        (self.global_dir / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        # repo1 already has it
        (self.repos[0] / ".claude" / "apiary" / "flags").mkdir(parents=True)
        (self.repos[0] / ".claude" / "apiary" / "flags" / "budgeter-log-enabled").write_text(
            "on", encoding="utf-8",
        )
        plan = mig.plan_copies(self.apiary, global_dir=self.global_dir)
        # Only repo2 needs the copy
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][1], "repo2")

    def test_repo_without_pin_dir_is_skipped(self):
        # repo2's .claude/apiary/ removed → not phase-2-bootstrapped
        import shutil
        shutil.rmtree(self.repos[1] / ".claude")
        (self.global_dir / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        plan = mig.plan_copies(self.apiary, global_dir=self.global_dir)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][1], "repo1")

    def test_apply_writes_files_and_is_idempotent(self):
        (self.global_dir / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        (self.global_dir / "budgeter-warn-enabled").write_text("on", encoding="utf-8")
        plan = mig.plan_copies(self.apiary, global_dir=self.global_dir)
        self.assertEqual(len(plan), 4)
        self.assertEqual(mig.apply_copies(plan), 4)
        # Re-plan: nothing left to do
        plan2 = mig.plan_copies(self.apiary, global_dir=self.global_dir)
        self.assertEqual(plan2, [])

    def test_main_dry_run_does_not_write(self):
        (self.global_dir / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        rc = mig.main([
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        # No per-repo flags written
        for repo in self.repos:
            flags = repo / ".claude" / "apiary" / "flags"
            if flags.is_dir():
                self.assertEqual(list(flags.iterdir()), [])

    def test_main_apply_writes(self):
        (self.global_dir / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        rc = mig.main([
            "--apply",
            "--apiary-repo", str(self.apiary),
            "--global-dir", str(self.global_dir),
        ])
        self.assertEqual(rc, 0)
        for repo in self.repos:
            self.assertTrue((repo / ".claude" / "apiary" / "flags" / "budgeter-log-enabled").is_file())


if __name__ == "__main__":
    unittest.main()
