#!/usr/bin/env python3
"""Tests for observer.failure — run_observer harness, jsonl write, failure-path contract."""
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

    def _read_error_log(self) -> str:
        log_path = self.tmp_dir / ".apiary" / "observer.log"
        if not log_path.is_file():
            return ""
        return log_path.read_text(encoding="utf-8")

    def _read_jsonl_lines(self) -> list[dict]:
        obs_dir = self.tmp_dir / ".apiary" / "observer"
        if not obs_dir.is_dir():
            return []
        lines: list[dict] = []
        for date_dir in sorted(obs_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for f in sorted(date_dir.iterdir()):
                if f.suffix != ".jsonl":
                    continue
                for raw in f.read_text(encoding="utf-8").splitlines():
                    if raw.strip():
                        lines.append(json.loads(raw))
        return lines

    def _jsonl_paths(self) -> list[Path]:
        obs_dir = self.tmp_dir / ".apiary" / "observer"
        if not obs_dir.is_dir():
            return []
        return sorted(
            p
            for date_dir in obs_dir.iterdir() if date_dir.is_dir()
            for p in date_dir.iterdir() if p.suffix == ".jsonl"
        )


class TestRunObserverSuccess(_Harness):

    def test_handler_returning_dict_writes_jsonl_and_returns_zero(self):
        def handler(event: ObservationEvent) -> dict:
            return {"note": "captured", "tool": event.tool_name}

        with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", handler)

        self.assertEqual(rc, 0)
        self.assertEqual(self._read_error_log(), "")
        self.assertEqual(self._blocker_calls, [])

        lines = self._read_jsonl_lines()
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertEqual(entry["observer"], "test-obs")
        self.assertEqual(entry["summary"], {"note": "captured", "tool": "Grep"})
        self.assertEqual(entry["schema_version"], 1)
        self.assertEqual(entry["event"]["session_id"], "sess-1")
        self.assertEqual(entry["event"]["tool_name"], "Grep")
        self.assertEqual(entry["event"]["raw"]["tool_input"], {"pattern": "foo"})
        self.assertIn("timestamp", entry)

    def test_handler_returning_none_writes_no_jsonl(self):
        with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", lambda e: None)
        self.assertEqual(rc, 0)
        self.assertEqual(self._read_error_log(), "")
        self.assertEqual(self._read_jsonl_lines(), [])

    def test_jsonl_path_shape_is_date_session(self):
        with self._patch_stdin(json.dumps(_valid_payload(session_id="abc123")).encode("utf-8")):
            failure_mod.run_observer("test-obs", lambda e: {"x": 1})
        paths = self._jsonl_paths()
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].name, "abc123.jsonl")
        self.assertRegex(paths[0].parent.name, r"^\d{4}-\d{2}-\d{2}$")

    def test_multiple_appends_to_same_session_file(self):
        for i in range(3):
            with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
                failure_mod.run_observer("test-obs", lambda e, i=i: {"i": i})
        lines = self._read_jsonl_lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([l["summary"]["i"] for l in lines], [0, 1, 2])
        self.assertEqual(len(self._jsonl_paths()), 1)

    def test_missing_session_id_falls_back_to_unknown(self):
        payload = _valid_payload()
        del payload["session_id"]
        with self._patch_stdin(json.dumps(payload).encode("utf-8")):
            failure_mod.run_observer("test-obs", lambda e: {"ok": True})
        paths = self._jsonl_paths()
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].name, "unknown-session.jsonl")

    def test_non_serialisable_tool_response_does_not_crash(self):
        class Unserialisable:
            pass

        def handler(event: ObservationEvent) -> dict:
            return {"ok": True}

        # Inject a non-JSONable tool_response after adapter parsing by patching from_payload.
        payload = _valid_payload()
        with self._patch_stdin(json.dumps(payload).encode("utf-8")):
            real_from_payload = failure_mod.from_payload

            def patched(p):
                ev = real_from_payload(p)
                ev.raw.tool_response = Unserialisable()
                return ev

            with mock.patch.object(failure_mod, "from_payload", side_effect=patched):
                rc = failure_mod.run_observer("test-obs", handler)
        self.assertEqual(rc, 0)
        self.assertEqual(self._read_error_log(), "")
        self.assertEqual(len(self._read_jsonl_lines()), 1)


class TestRunObserverFailure(_Harness):

    def test_handler_exception_writes_log_and_files_blocker_and_returns_zero(self):
        def handler(event: ObservationEvent) -> dict:
            raise RuntimeError("boom")

        with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", handler)

        self.assertEqual(rc, 0)
        log = self._read_error_log()
        self.assertIn("observer=test-obs", log)
        self.assertIn("RuntimeError", log)
        self.assertIn("boom", log)
        self.assertIn("raw_payload=", log)
        self.assertEqual(len(self._blocker_calls), 1)
        self.assertEqual(self._read_jsonl_lines(), [])

    def test_empty_stdin_writes_log_and_returns_zero(self):
        called = []

        with self._patch_stdin(b""):
            rc = failure_mod.run_observer("test-obs", lambda e: called.append(e))

        self.assertEqual(rc, 0)
        self.assertEqual(called, [])
        log = self._read_error_log()
        self.assertIn("AdapterError", log)
        self.assertEqual(len(self._blocker_calls), 1)

    def test_malformed_json_writes_log_and_returns_zero(self):
        with self._patch_stdin(b"{not json"):
            rc = failure_mod.run_observer("test-obs", lambda e: None)
        self.assertEqual(rc, 0)
        log = self._read_error_log()
        self.assertIn("AdapterError", log)

    def test_missing_hook_event_name_writes_log_and_returns_zero(self):
        payload = _valid_payload()
        del payload["hook_event_name"]
        with self._patch_stdin(json.dumps(payload).encode("utf-8")):
            rc = failure_mod.run_observer("test-obs", lambda e: None)
        self.assertEqual(rc, 0)
        log = self._read_error_log()
        self.assertIn("AdapterError", log)

    def test_log_append_survives_repeat_failures(self):
        def handler(event: ObservationEvent) -> dict:
            raise ValueError("x")

        for _ in range(3):
            with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
                failure_mod.run_observer("test-obs", handler)

        log = self._read_error_log()
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
            log = self._read_error_log()
            self.assertIn("ZeroDivisionError", log)
        finally:
            patch.stop()
            self._blocker_patch.start()

    def test_jsonl_write_failure_routes_to_blocker(self):
        def handler(event: ObservationEvent) -> dict:
            return {"ok": True}

        with mock.patch.object(
            failure_mod,
            "_append_observation_log",
            side_effect=OSError("disk full"),
        ):
            with self._patch_stdin(json.dumps(_valid_payload()).encode("utf-8")):
                rc = failure_mod.run_observer("test-obs", handler)
        self.assertEqual(rc, 0)
        self.assertIn("OSError", self._read_error_log())
        self.assertIn("disk full", self._read_error_log())


if __name__ == "__main__":
    unittest.main()
