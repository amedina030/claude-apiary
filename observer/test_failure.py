#!/usr/bin/env python3
"""Tests for observer.failure — run_observer harness and failure-path contract."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observer import failure as failure_mod
from observer.adapter import ObservationEvent


def _valid_payload(**overrides) -> dict:
    payload = {
        "session_id": "sess-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Grep",
        "tool_use_id": "tu-1",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/repo",
        "permission_mode": "default",
        "tool_input": {"pattern": "foo"},
        "tool_response": {"matches": 3},
    }
    payload.update(overrides)
    return payload


class _Harness(unittest.TestCase):
    """Shared setup: tempdir as cwd, blocker subprocess suppressed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp_dir)
        self._blocker_calls = []
        self._blocker_patch = mock.patch.object(
            failure_mod,
            "_file_scribe_blocker",
            side_effect=lambda *a, **kw: self._blocker_calls.append((a, kw)),
        )
        self._blocker_patch.start()

    def tearDown(self):
        self._blocker_patch.stop()
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _patch_stdin(self, data: bytes):
        fake = io.BytesIO(data)
        return mock.patch.object(sys, "stdin", mock.Mock(buffer=fake))

    def _read_log(self) -> str:
        log_path = self.tmp_dir / ".apiary" / "observer.log"
        if not log_path.is_file():
            return ""
        return log_path.read_text(encoding="utf-8")


class TestRunObserverSuccess(_Harness):

    def test_handler_receives_event_and_exit_code_is_zero(self):
        seen = []

        def handler(event: ObservationEvent) -> None:
            seen.append(event)

        with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", handler)

        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].tool_name, "Grep")
        self.assertEqual(self._read_log(), "")
        self.assertEqual(self._blocker_calls, [])

    def test_handler_that_returns_none_leaves_log_empty(self):
        with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", lambda e: None)
        self.assertEqual(rc, 0)
        self.assertEqual(self._read_log(), "")


class TestRunObserverFailure(_Harness):

    def test_handler_exception_writes_log_and_files_blocker_and_returns_zero(self):
        def handler(event: ObservationEvent) -> None:
            raise RuntimeError("boom")

        with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", handler)

        self.assertEqual(rc, 0)
        log = self._read_log()
        self.assertIn("observer=test-obs", log)
        self.assertIn("RuntimeError", log)
        self.assertIn("boom", log)
        self.assertIn("raw_payload=", log)
        self.assertEqual(len(self._blocker_calls), 1)

    def test_empty_stdin_writes_log_and_returns_zero(self):
        called = []

        with self._patch_stdin(b""):
            rc = failure_mod.run_observer("test-obs", lambda e: called.append(e))

        self.assertEqual(rc, 0)
        self.assertEqual(called, [])
        log = self._read_log()
        self.assertIn("AdapterError", log)
        self.assertEqual(len(self._blocker_calls), 1)

    def test_malformed_json_writes_log_and_returns_zero(self):
        with self._patch_stdin(b"{not json"):
            rc = failure_mod.run_observer("test-obs", lambda e: None)
        self.assertEqual(rc, 0)
        log = self._read_log()
        self.assertIn("AdapterError", log)

    def test_missing_hook_event_name_writes_log_and_returns_zero(self):
        payload = _valid_payload()
        del payload["hook_event_name"]
        with self._patch_stdin(json.dumps(payload).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", lambda e: None)
        self.assertEqual(rc, 0)
        log = self._read_log()
        self.assertIn("AdapterError", log)

    def test_log_append_survives_repeat_failures(self):
        def handler(event: ObservationEvent) -> None:
            raise ValueError("x")

        for _ in range(3):
            with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
                failure_mod.run_observer("test-obs", handler)

        log = self._read_log()
        self.assertEqual(log.count("observer=test-obs"), 3)
        self.assertEqual(log.count("---"), 3)

    def test_blocker_failure_does_not_propagate(self):
        self._blocker_patch.stop()
        patch = mock.patch.object(
            failure_mod,
            "_file_scribe_blocker",
            side_effect=OSError("scribe unreachable"),
        )
        patch.start()
        try:
            with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
                rc = failure_mod.run_observer("test-obs", lambda e: 1 / 0)
            self.assertEqual(rc, 0)
            log = self._read_log()
            self.assertIn("ZeroDivisionError", log)
        finally:
            patch.stop()
            self._blocker_patch.start()


if __name__ == "__main__":
    unittest.main()
