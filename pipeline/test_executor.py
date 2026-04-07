#!/usr/bin/env python3
"""Tests for pipeline/executor.py helpers.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
Focused on pure helpers; full main() integration is exercised by live
pipeline runs.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from executor import load_previous_log


class TestLoadPreviousLog(unittest.TestCase):
    """load_previous_log lets executor.main() preserve per-step status from
    prior runs on resume — especially verify/test steps, which don't commit
    and therefore can't be recovered from git log alone (item D of the
    pipeline-executor-architecture-hardening-from-t4-failures ticket)."""

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
                    "files_changed": ["pipeline/x.py"],
                    "duration_ms": 12345,
                    "custom": "value",
                },
            ],
        })
        entry = load_previous_log(log_path)[5]
        self.assertEqual(entry["duration_ms"], 12345)
        self.assertEqual(entry["custom"], "value")
        self.assertEqual(entry["files_changed"], ["pipeline/x.py"])


if __name__ == "__main__":
    unittest.main()
