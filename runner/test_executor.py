#!/usr/bin/env python3
"""Tests for runner/executor.py helpers.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
Focused on pure helpers; full main() integration is exercised by live
runner runs.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import executor
from executor import execute_step, load_previous_log


class TestLoadPreviousLog(unittest.TestCase):
    """load_previous_log lets executor.main() preserve per-step status from
    prior runs on resume — especially verify/test steps, which don't commit
    and therefore can't be recovered from git log alone (item D of the
    runner-executor-architecture-hardening-from-t4-failures ticket)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_log(self, payload):
        log_path = self.tmp_path / "log.json"
        log_path.write_text(json.dumps(payload), encoding="utf-8")
        return log_path

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_previous_log(self.tmp_path / "nope.json"), {})

    def test_malformed_json_returns_empty(self):
        log_path = self.tmp_path / "log.json"
        log_path.write_text("not json {{{", encoding="utf-8")
        self.assertEqual(load_previous_log(log_path), {})

    def test_empty_steps_returns_empty(self):
        log_path = self._write_log({"uuid": "x", "steps": []})
        self.assertEqual(load_previous_log(log_path), {})

    def test_keyed_by_step_number(self):
        log_path = self._write_log({
            "uuid": "x",
            "steps": [
                {"step_number": 1, "status": "passed"},
                {"step_number": 2, "status": "failed", "error": "boom"},
            ],
        })
        result = load_previous_log(log_path)
        self.assertEqual(set(result.keys()), {1, 2})
        self.assertEqual(result[2]["error"], "boom")

    def test_skips_entries_without_int_step_number(self):
        log_path = self._write_log({
            "steps": [
                {"step_number": 1, "status": "passed"},
                {"step_number": "two", "status": "passed"},  # bad type
                "not-a-dict",  # not a dict
                {"status": "passed"},  # missing step_number
            ],
        })
        self.assertEqual(set(load_previous_log(log_path).keys()), {1})

    def test_preserves_full_entry(self):
        # Carried-forward entries should retain rich fields the bland
        # stub doesn't have (e.g. tool_uses, duration, custom keys)
        log_path = self._write_log({
            "steps": [
                {
                    "step_number": 5,
                    "status": "passed",
                    "files_changed": ["runner/x.py"],
                    "duration_ms": 12345,
                    "custom": "value",
                },
            ],
        })
        entry = load_previous_log(log_path)[5]
        self.assertEqual(entry["duration_ms"], 12345)
        self.assertEqual(entry["custom"], "value")
        self.assertEqual(entry["files_changed"], ["runner/x.py"])


class TestExecuteStepTestAction(unittest.TestCase):
    """execute_step('test', ...) must not retry on failure (test files
    don't change between attempts in the same run, so retries waste a slot)
    and must store the FULL test output (not a truncated prefix) so
    diagnosis isn't blocked by mid-traceback cutoffs. Both behaviors guard
    sub-bugs (2) and (3) from TODO #197."""

    def test_failed_test_action_does_not_retry(self):
        step = {"step_number": 1, "action": "test", "code_spec": "fail-me", "files": []}
        call_count = {"n": 0}

        def fake_run(_):
            call_count["n"] += 1
            return False, "boom"

        with mock.patch.object(executor, "run_test_command", side_effect=fake_run):
            result = execute_step(step, spec={}, model="sonnet")

        self.assertEqual(call_count["n"], 1, "test action must not retry")
        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["error"])

    def test_failed_test_action_stores_full_output(self):
        # Long traceback-shaped output — must be preserved verbatim, not
        # truncated at 500 chars (the previous behavior cut tracebacks
        # mid-line at 'File "D:\\Professional\\claude-' on T4).
        long_output = "Traceback line " + ("X" * 2000)
        step = {"step_number": 1, "action": "test", "code_spec": "x", "files": []}

        with mock.patch.object(executor, "run_test_command", return_value=(False, long_output)):
            result = execute_step(step, spec={}, model="sonnet")

        self.assertIn(long_output, result["error"])
        self.assertGreater(len(result["error"]), 1500)


if __name__ == "__main__":
    unittest.main()
