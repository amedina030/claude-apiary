#!/usr/bin/env python3
"""Tests for runner/run_lock.py (T-2026-128)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from runner import run_lock


class WriteLockTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig = run_lock.LOCKS_DIR
        run_lock.LOCKS_DIR = Path(self.tmpdir.name) / "locks"

    def tearDown(self):
        run_lock.LOCKS_DIR = self._orig
        self.tmpdir.cleanup()

    def test_write_creates_lockfile(self):
        path = run_lock.write("test-uuid-1", stage="auto_plan", step_number=3)
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["stage"], "auto_plan")
        self.assertEqual(data["step_number"], 3)
        self.assertIn("hostname", data)
        self.assertIn("started_at", data)

    def test_update_changes_stage_and_step(self):
        run_lock.write("test-uuid-2", stage="init", step_number=0)
        run_lock.update("test-uuid-2", stage="executor", step_number=4)
        data = run_lock.read("test-uuid-2")
        self.assertEqual(data["stage"], "executor")
        self.assertEqual(data["step_number"], 4)
        self.assertEqual(data["pid"], os.getpid())

    def test_update_nonexistent_is_noop(self):
        run_lock.update("no-such-uuid", stage="x")

    def test_delete_removes_lockfile(self):
        run_lock.write("test-uuid-3")
        run_lock.delete("test-uuid-3")
        self.assertIsNone(run_lock.read("test-uuid-3"))

    def test_delete_nonexistent_is_noop(self):
        run_lock.delete("no-such-uuid")

    def test_read_corrupt_returns_none(self):
        run_lock.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        bad = run_lock.LOCKS_DIR / "corrupt.lock"
        bad.write_text("not json!!!", encoding="utf-8")
        self.assertIsNone(run_lock.read("corrupt"))


class StalenessTests(unittest.TestCase):
    def test_dead_pid_is_stale(self):
        data = {"pid": 999999999, "started_at": time.time(), "stage": "x"}
        with mock.patch.object(run_lock, "_pid_alive", return_value=False):
            self.assertTrue(run_lock.is_stale(data))

    def test_alive_pid_within_timeout_is_not_stale(self):
        data = {"pid": os.getpid(), "started_at": time.time(), "stage": "x"}
        self.assertFalse(run_lock.is_stale(data))

    def test_alive_pid_past_timeout_is_stale(self):
        data = {"pid": os.getpid(), "started_at": time.time() - 99999, "stage": "x"}
        with mock.patch("runner.run_lock.cfg", return_value=100):
            self.assertTrue(run_lock.is_stale(data))


class ScanStaleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig = run_lock.LOCKS_DIR
        run_lock.LOCKS_DIR = Path(self.tmpdir.name) / "locks"

    def tearDown(self):
        run_lock.LOCKS_DIR = self._orig
        self.tmpdir.cleanup()

    def test_empty_dir_returns_empty(self):
        self.assertEqual(run_lock.scan_stale(), [])

    def test_finds_stale_lockfile(self):
        run_lock.write("stale-1", stage="plan")
        with mock.patch.object(run_lock, "is_stale", return_value=True):
            results = run_lock.scan_stale()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["uuid"], "stale-1")

    def test_skips_live_lockfile(self):
        run_lock.write("live-1", stage="plan")
        with mock.patch.object(run_lock, "is_stale", return_value=False):
            results = run_lock.scan_stale()
        self.assertEqual(len(results), 0)

    def test_corrupt_lockfile_included(self):
        run_lock.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        (run_lock.LOCKS_DIR / "corrupt.lock").write_text("bad", encoding="utf-8")
        results = run_lock.scan_stale()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].get("_corrupt"))


class PidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(run_lock._pid_alive(os.getpid()))

    def test_invalid_pid_not_alive(self):
        self.assertFalse(run_lock._pid_alive(-1))
        self.assertFalse(run_lock._pid_alive(0))

    def test_nonexistent_pid_not_alive(self):
        self.assertFalse(run_lock._pid_alive(999999999))


if __name__ == "__main__":
    unittest.main()
