#!/usr/bin/env python3
"""Tests for core/hooks/compass_pair_log.py — the Stop hook that logs turn pairs.

In-process tests drive ``run(payload)`` against a temporary state dir; one
subprocess test runs the standalone shim the way settings.json would, with a
hermetic environment, and asserts it prints a no-objection reply.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from compass import store  # noqa: E402
from core.hooks import compass_pair_log, dispatch  # noqa: E402
from core.testing import hermetic_env  # noqa: E402

HOOK = Path(__file__).resolve().parent / "compass_pair_log.py"
SID = "abcd1234-1111-2222-3333-444444444444"


def _user(prompt_id: str, text: str) -> dict:
    return {
        "type": "user",
        "promptId": prompt_id,
        "timestamp": "2026-09-06T00:00:00Z",
        "entrypoint": "cli",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
    }


def _asst(text: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-09-06T00:00:01Z",
        "entrypoint": "cli",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


class CompassPairLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.transcript = self.root / "session.jsonl"
        self.transcript.write_text(
            "".join(json.dumps(r) + "\n" for r in (_asst("A0"), _user("p01", "U1"), _asst("A1"))),
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            os.environ,
            {store.TARGET_STATE_DIR_ENV: str(self.state), "APIARY_RUNNER_SUBPROCESS": ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("APIARY_RUNNER_SUBPROCESS", None)

    def payload(self) -> dict:
        return {"session_id": SID, "transcript_path": str(self.transcript)}

    def test_logs_pairs_and_adds_no_context(self):
        self.assertIsNone(compass_pair_log.run(self.payload()))
        rows = store.turns_path(SID).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["user"], "U1")
        self.assertTrue(store.cursor_path(SID).is_file())

    def test_runner_subprocess_is_skipped(self):
        with mock.patch.dict(os.environ, {"APIARY_RUNNER_SUBPROCESS": "1"}):
            self.assertIsNone(compass_pair_log.run(self.payload()))
        self.assertFalse(store.turns_path(SID).exists())

    def test_incomplete_payload_or_missing_transcript_is_a_no_op(self):
        self.assertIsNone(compass_pair_log.run({"session_id": SID}))
        self.assertIsNone(compass_pair_log.run({"transcript_path": str(self.transcript)}))
        self.assertIsNone(
            compass_pair_log.run(
                {"session_id": SID, "transcript_path": str(self.root / "nope.jsonl")}
            )
        )
        self.assertFalse(store.turns_path(SID).exists())

    def test_registered_in_the_stop_chain_after_save_transcript(self):
        names = [h.name for h in dispatch._registry()["Stop"]]
        self.assertIn("compass_pair_log", names)
        self.assertGreater(names.index("compass_pair_log"), names.index("save_transcript"))

    def test_standalone_shim_prints_no_objection(self):
        env = hermetic_env(
            HOME=str(self.root),
            USERPROFILE=str(self.root),
            APIARY_TARGET_STATE_DIR=str(self.state),
            APIARY_RUNNER_SUBPROCESS="",
        )
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(self.payload()),
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout.strip() or "{}"), {})
        self.assertTrue(store.turns_path(SID).is_file(), msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
