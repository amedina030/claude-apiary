"""Tests for runner.run_tracker — cross-invocation run tracking."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import run_tracker


class TestRunTracker(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self.tmp.name) / "runs"
        self._orig_runs_dir = run_tracker.RUNS_DIR
        run_tracker.RUNS_DIR = self.runs_dir

    def tearDown(self):
        run_tracker.RUNS_DIR = self._orig_runs_dir
        self.tmp.cleanup()

    def test_load_missing_returns_empty(self):
        self.assertEqual(run_tracker.load("nonexistent"), {})

    def test_save_and_load_roundtrip(self):
        data = {"uuid": "abc", "total_tokens": 500, "attempts": []}
        run_tracker.save("abc", data)
        loaded = run_tracker.load("abc")
        self.assertEqual(loaded, data)

    def test_save_creates_directory(self):
        self.assertFalse(self.runs_dir.exists())
        run_tracker.save("x", {"uuid": "x"})
        self.assertTrue(self.runs_dir.exists())

    def test_record_attempt_creates_tracker(self):
        tracker = run_tracker.record_attempt(
            "uuid1", tokens_this_run=100000, stages_completed=3,
            exit_status="stage_failed:executor", last_stage_completed="auto_plan",
        )
        self.assertEqual(tracker["uuid"], "uuid1")
        self.assertEqual(tracker["total_tokens"], 100000)
        self.assertEqual(tracker["attempt_count"], 1)
        self.assertEqual(tracker["last_exit_status"], "stage_failed:executor")
        self.assertEqual(tracker["last_stage_completed"], "auto_plan")
        self.assertEqual(len(tracker["attempts"]), 1)

    def test_record_attempt_accumulates(self):
        run_tracker.record_attempt("u", 100, 2, "stage_failed:plan")
        tracker = run_tracker.record_attempt("u", 200, 4, "stage_failed:harden")
        self.assertEqual(tracker["total_tokens"], 300)
        self.assertEqual(tracker["attempt_count"], 2)
        self.assertEqual(len(tracker["attempts"]), 2)
        self.assertEqual(tracker["attempts"][0]["tokens"], 100)
        self.assertEqual(tracker["attempts"][1]["tokens"], 200)

    def test_delete_removes_file(self):
        run_tracker.save("del-me", {"uuid": "del-me"})
        self.assertTrue((self.runs_dir / "del-me.json").exists())
        run_tracker.delete("del-me")
        self.assertFalse((self.runs_dir / "del-me.json").exists())

    def test_delete_nonexistent_is_noop(self):
        run_tracker.delete("never-existed")  # should not raise

    def test_load_corrupt_file_returns_empty(self):
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.runs_dir / "bad.json").write_text("not json!", encoding="utf-8")
        self.assertEqual(run_tracker.load("bad"), {})


class TestGetResumeStage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)
        # Create runner artifact directories
        for d in ("intake", "specs", "plans", "executions", "hardens", "reports"):
            (self.wt / "runner" / d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_artifacts_returns_none(self):
        self.assertIsNone(run_tracker.get_resume_stage("uuid1", self.wt))

    def test_spec_exists_resumes_from_auto_plan(self):
        (self.wt / "runner" / "specs" / "uuid1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(run_tracker.get_resume_stage("uuid1", self.wt), "auto_plan")

    def test_plan_exists_resumes_from_executor(self):
        (self.wt / "runner" / "specs" / "uuid1.json").write_text("{}", encoding="utf-8")
        (self.wt / "runner" / "plans" / "uuid1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(run_tracker.get_resume_stage("uuid1", self.wt), "executor")

    def test_execution_exists_resumes_from_auto_harden(self):
        for d in ("specs", "plans", "executions"):
            (self.wt / "runner" / d / "uuid1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(run_tracker.get_resume_stage("uuid1", self.wt), "auto_harden")

    def test_harden_exists_resumes_from_approval(self):
        for d in ("specs", "plans", "executions", "hardens"):
            (self.wt / "runner" / d / "uuid1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(run_tracker.get_resume_stage("uuid1", self.wt), "approval")

    def test_picks_latest_artifact(self):
        """When only a plan exists (no execution), should resume from executor."""
        (self.wt / "runner" / "plans" / "uuid1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(run_tracker.get_resume_stage("uuid1", self.wt), "executor")


if __name__ == "__main__":
    unittest.main()
