#!/usr/bin/env python3
"""Tests for core/hooks/save_transcript.py.

Runs the hook as a subprocess with a temporary HOME and asserts that:
  - A normal session ends up in .session-history.json
  - A runner subprocess session (APIARY_RUNNER_SUBPROCESS=1) does NOT
    end up in .session-history.json or .last-session.json (#223)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "save_transcript.py"
PYTHON = sys.executable


def _run_hook(payload: dict, home: Path, *, runner_subprocess: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if runner_subprocess:
        env["APIARY_RUNNER_SUBPROCESS"] = "1"
    else:
        env.pop("APIARY_RUNNER_SUBPROCESS", None)
    return subprocess.run(
        [PYTHON, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )


class TestSaveTranscript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.history = self.home / ".claude" / ".session-history.json"
        self.last = self.home / ".claude" / ".last-session.json"

    def _payload(self, sid: str = "abcd1234-1111-2222-3333-444444444444") -> dict:
        return {
            "session_id": sid,
            "transcript_path": str(self.home / ".claude" / "projects" / "demo" / f"{sid}.jsonl"),
        }

    def test_normal_session_logged(self):
        # The hook calls load_identity which reads from sid.identity_path() —
        # for a session with no identity file, it returns defaults. Verify
        # the hook completes and writes history.
        result = _run_hook(self._payload(), self.home)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.history.exists(), msg=result.stderr)
        history = json.loads(self.history.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["session_id"], "abcd1234-1111-2222-3333-444444444444")
        self.assertTrue(self.last.exists())

    def test_runner_subprocess_skipped(self):
        result = _run_hook(self._payload(), self.home, runner_subprocess=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.history.exists(), "history file should not be created")
        self.assertFalse(self.last.exists(), ".last-session.json should not be created")

    def test_runner_subprocess_does_not_pollute_existing_history(self):
        # Pre-seed history with one entry
        _run_hook(self._payload("aaaaaaaa-1111-2222-3333-444444444444"), self.home)
        # Now run a "subprocess" session — it should be ignored
        result = _run_hook(
            self._payload("bbbbbbbb-1111-2222-3333-444444444444"),
            self.home,
            runner_subprocess=True,
        )
        self.assertEqual(result.returncode, 0)
        history = json.loads(self.history.read_text(encoding="utf-8"))
        ids = [h["session_id"] for h in history]
        self.assertIn("aaaaaaaa-1111-2222-3333-444444444444", ids)
        self.assertNotIn("bbbbbbbb-1111-2222-3333-444444444444", ids)


if __name__ == "__main__":
    unittest.main()
