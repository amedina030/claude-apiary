#!/usr/bin/env python3
"""Tests for core/hooks/save_transcript.py.

Runs the hook as a subprocess with a temporary HOME and a temporary
state dir (APIARY_TARGET_STATE_DIR) and asserts that:
  - A normal session ends up in <state-dir>/sessions/history.json
  - A runner subprocess session (APIARY_RUNNER_SUBPROCESS=1) does NOT
    end up in history.json or last-session.json (#223)
  - Nothing is written under ~/.claude (review S1)
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.testing import hermetic_env  # noqa: E402

HOOK = Path(__file__).resolve().parent / "save_transcript.py"
PYTHON = sys.executable


def _run_hook(
    payload: dict, home: Path, *, runner_subprocess: bool = False
) -> subprocess.CompletedProcess:
    # hermetic_env, not os.environ.copy(): a live session exports
    # CLAUDE_PROJECT_DIR and APIARY_* pointing at the real checkout, and the
    # hook under test would resolve those instead of this tmpdir.
    env = hermetic_env(
        HOME=str(home),
        USERPROFILE=str(home),
        APIARY_TARGET_STATE_DIR=str(home / "state"),
        APIARY_RUNNER_SUBPROCESS="1" if runner_subprocess else "",
    )
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
        self.home = Path(self._tmp.name).resolve()
        self.state = self.home / "state"
        self.history = self.state / "sessions" / "history.json"
        self.last = self.state / "sessions" / "last-session.json"

    def _home_claude_files(self):
        root = self.home / ".claude"
        return (
            sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
            if root.exists()
            else []
        )

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
        raw = json.loads(self.history.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)  # the documented v1 shape
        history = raw["sessions"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["session_id"], "abcd1234-1111-2222-3333-444444444444")
        self.assertTrue(self.last.exists())
        self.assertEqual(self._home_claude_files(), [], "apiary must write nothing under ~/.claude")

    def test_legacy_v1_history_is_read_and_kept(self):
        # The 2026-05 migration left {"schema_version": 1, "sessions": [...]}
        # files in .repos/<slug>/sessions/; the hook must append to them, and
        # a malformed entry must not break the ring buffer.
        self.history.parent.mkdir(parents=True)
        self.history.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sessions": [
                        {
                            "session_id": "11111111-1111-2222-3333-444444444444",
                            "ended_at": "2026-05-05T00:00:00Z",
                        },
                        "garbage",
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = _run_hook(self._payload(), self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        sessions = json.loads(self.history.read_text(encoding="utf-8"))["sessions"]
        self.assertEqual([s["session_id"][:8] for s in sessions], ["11111111", "abcd1234"])

    def test_unwritable_state_dir_does_not_crash_the_hook(self):
        # A Stop hook that exits non-zero surfaces a hook error every turn.
        blocker = self.home / "state"
        blocker.write_text("not a directory", encoding="utf-8")
        result = _run_hook(self._payload(), self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("save_transcript", result.stderr)

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
        history = json.loads(self.history.read_text(encoding="utf-8"))["sessions"]
        ids = [h["session_id"] for h in history]
        self.assertIn("aaaaaaaa-1111-2222-3333-444444444444", ids)
        self.assertNotIn("bbbbbbbb-1111-2222-3333-444444444444", ids)


if __name__ == "__main__":
    unittest.main()
