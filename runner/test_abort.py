#!/usr/bin/env python3
"""Tests for runner/abort.py (T-2026-128)."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from runner import abort as abort_mod
from runner import run_lock


class ArchiveArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self._orig_root = abort_mod.ARTIFACTS_ROOT
        self._orig_crashes = abort_mod.CRASHES_DIR
        self._orig_locks = run_lock.LOCKS_DIR
        abort_mod.ARTIFACTS_ROOT = self.tmp
        abort_mod.CRASHES_DIR = self.tmp / "crashes"
        run_lock.LOCKS_DIR = self.tmp / "locks"

    def tearDown(self):
        abort_mod.ARTIFACTS_ROOT = self._orig_root
        abort_mod.CRASHES_DIR = self._orig_crashes
        run_lock.LOCKS_DIR = self._orig_locks
        self.tmpdir.cleanup()

    def test_archives_existing_artifacts(self):
        specs = self.tmp / "specs"
        specs.mkdir()
        (specs / "abc.json").write_text('{"test": 1}', encoding="utf-8")
        plans = self.tmp / "plans"
        plans.mkdir()
        (plans / "abc.json").write_text('{"steps": []}', encoding="utf-8")

        dest = abort_mod._archive_artifacts("abc")
        self.assertTrue(dest.exists())
        self.assertTrue((dest / "specs_abc.json").exists())
        self.assertTrue((dest / "plans_abc.json").exists())
        self.assertFalse((dest / "executions_abc.json").exists())

    def test_archives_lockfile_data(self):
        run_lock.write("def", stage="plan")
        dest = abort_mod._archive_artifacts("def")
        self.assertTrue((dest / "lockfile.json").exists())

    def test_empty_archive_when_no_artifacts(self):
        dest = abort_mod._archive_artifacts("none")
        self.assertTrue(dest.exists())
        self.assertEqual(list(dest.iterdir()), [])

    def test_reads_the_state_dir_not_the_source_tree(self):
        """review runner Bug 11 / T-2026-278a: _archive_artifacts used to read
        `runner/<dir>/<uuid>.json` inside the checkout — the pre-migration
        location — so every abort archived nothing."""
        from runner.target_repo import artifacts_root
        self.assertEqual(Path(self._orig_root), artifacts_root())
        self.assertNotEqual(
            Path(self._orig_root).resolve(),
            Path(abort_mod.__file__).resolve().parent,
            "artifacts must not be read out of the source tree",
        )
        self.assertEqual(
            Path(abort_mod.CRASHES_DIR).name, "crashes",
        )


class AbortRunTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self._orig_root = abort_mod.ARTIFACTS_ROOT
        self._orig_crashes = abort_mod.CRASHES_DIR
        self._orig_locks = run_lock.LOCKS_DIR
        abort_mod.ARTIFACTS_ROOT = self.tmp
        abort_mod.CRASHES_DIR = self.tmp / "crashes"
        run_lock.LOCKS_DIR = self.tmp / "locks"

    def tearDown(self):
        abort_mod.ARTIFACTS_ROOT = self._orig_root
        abort_mod.CRASHES_DIR = self._orig_crashes
        run_lock.LOCKS_DIR = self._orig_locks
        self.tmpdir.cleanup()

    @mock.patch.object(abort_mod, "_delete_branches", return_value=[])
    @mock.patch.object(abort_mod, "_remove_worktree", return_value=True)
    def test_abort_stale_run(self, mock_wt, mock_br):
        run_lock.write("stale-1", stage="executor", step_number=4)
        with mock.patch.object(run_lock, "is_stale", return_value=True):
            summary = abort_mod.abort_run("stale-1")
        self.assertIn("stale-1", summary)
        self.assertIn("executor", summary)
        self.assertIsNone(run_lock.read("stale-1"))

    def test_abort_live_run_raises(self):
        run_lock.write("live-1", stage="plan")
        with mock.patch.object(run_lock, "is_stale", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                abort_mod.abort_run("live-1")
            self.assertIn("still active", str(ctx.exception))

    @mock.patch.object(abort_mod, "_delete_branches", return_value=[])
    @mock.patch.object(abort_mod, "_remove_worktree", return_value=True)
    def test_abort_corrupt_lockfile(self, mock_wt, mock_br):
        run_lock.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        (run_lock.LOCKS_DIR / "corrupt.lock").write_text("bad", encoding="utf-8")
        summary = abort_mod.abort_run("corrupt")
        self.assertIn("corrupt", summary)

    @mock.patch.object(abort_mod, "_delete_branches", return_value=[])
    @mock.patch.object(abort_mod, "_remove_worktree", return_value=True)
    def test_double_abort_skips_existing_archive(self, mock_wt, mock_br):
        run_lock.write("double-1", stage="plan")
        crash_dir = abort_mod.CRASHES_DIR / "double-1"
        crash_dir.mkdir(parents=True)
        (crash_dir / "old_file.txt").write_text("prior archive", encoding="utf-8")
        with mock.patch.object(run_lock, "is_stale", return_value=True):
            abort_mod.abort_run("double-1")
        self.assertTrue((crash_dir / "old_file.txt").exists())


class AbortAllTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self._orig_locks = run_lock.LOCKS_DIR
        self._orig_root = abort_mod.ARTIFACTS_ROOT
        self._orig_crashes = abort_mod.CRASHES_DIR
        run_lock.LOCKS_DIR = self.tmp / "locks"
        abort_mod.ARTIFACTS_ROOT = self.tmp
        abort_mod.CRASHES_DIR = self.tmp / "crashes"

    def tearDown(self):
        run_lock.LOCKS_DIR = self._orig_locks
        abort_mod.ARTIFACTS_ROOT = self._orig_root
        abort_mod.CRASHES_DIR = self._orig_crashes
        self.tmpdir.cleanup()

    def test_abort_all_empty(self):
        count = abort_mod.abort_all()
        self.assertEqual(count, 0)

    @mock.patch.object(abort_mod, "_delete_branches", return_value=[])
    @mock.patch.object(abort_mod, "_remove_worktree", return_value=True)
    def test_abort_all_multiple(self, mock_wt, mock_br):
        run_lock.write("s1", stage="plan")
        run_lock.write("s2", stage="executor")
        with mock.patch.object(run_lock, "is_stale", return_value=True):
            count = abort_mod.abort_all()
        self.assertEqual(count, 2)
        self.assertIsNone(run_lock.read("s1"))
        self.assertIsNone(run_lock.read("s2"))


class BuildSummaryTests(unittest.TestCase):
    def test_summary_with_lock_data(self):
        lock = {"stage": "plan", "step_number": 2, "pid": 1234,
                "started_at": time.time() - 600, "worktree_path": "/tmp/wt"}
        s = abort_mod._build_summary("uuid-1", lock, Path("/tmp/crashes/uuid-1"),
                                      ["runner/slug-uuid-1"], True)
        self.assertIn("uuid-1", s)
        self.assertIn("plan", s)
        self.assertIn("removed", s)
        self.assertIn("runner/slug-uuid-1", s)

    def test_summary_without_lock_data(self):
        s = abort_mod._build_summary("uuid-2", None, Path("/tmp/crashes/uuid-2"),
                                      [], True)
        self.assertIn("corrupt or missing", s)


if __name__ == "__main__":
    unittest.main()
