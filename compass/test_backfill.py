"""Tests for compass/backfill.py — transcript selection timestamps.

Hermetic: every test writes its own transcript into a tempdir and points
``APIARY_TARGET_STATE_DIR`` at another one. Nothing reads the operator's
real ``~/.claude/projects`` transcripts or the live compass store, and the
``claude`` subprocess is always mocked.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from compass import backfill, store


def _write_transcript(path: Path, mtime: float | None = None) -> Path:
    """Write a minimal four-message transcript and optionally set its mtime."""
    lines = [
        {"message": {"role": "user", "content": "add the retry loop"}},
        {"message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Done — three attempts."}]}},
        {"message": {"role": "user", "content": "why three and not two"}},
        {"message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Because the validator..."}]}},
    ]
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class CapturedAtTest(unittest.TestCase):
    """Review knowledge Bug 5: captured_at is the session's time, not now()."""

    def test_uses_transcript_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp) / "deadbeef.jsonl")
            when = datetime(2026, 4, 17, 20, 30, 15, tzinfo=timezone.utc)
            os.utime(path, (when.timestamp(), when.timestamp()))
            self.assertEqual(backfill._captured_at(path), "2026-04-17T20:30:15Z")

    def test_is_not_now_for_an_old_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp) / "deadbeef.jsonl")
            old = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
            os.utime(path, (old, old))
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.assertFalse(backfill._captured_at(path).startswith(today))

    def test_result_is_iso_8601_the_validator_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_transcript(Path(tmp) / "deadbeef.jsonl")
            captured = backfill._captured_at(path)
            payload = {
                "session_id": "deadbeef",
                "captured_at": captured,
                "observations": [],
            }
            self.assertEqual(store.validate_observation(payload), [])

    def test_falls_back_to_now_when_stat_fails(self):
        missing = Path(tempfile.gettempdir()) / "compass-no-such-transcript.jsonl"
        before = datetime.now(timezone.utc)
        got = datetime.fromisoformat(backfill._captured_at(missing).replace("Z", "+00:00"))
        self.assertLess(abs((got - before).total_seconds()), 60)

    def test_ordering_matches_transcript_age(self):
        # synthesize sorts on this string; lexical order must track real time.
        with tempfile.TemporaryDirectory() as tmp:
            older = _write_transcript(Path(tmp) / "aaaaaaaa.jsonl")
            newer = _write_transcript(Path(tmp) / "bbbbbbbb.jsonl")
            t_old = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()
            t_new = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
            os.utime(older, (t_old, t_old))
            os.utime(newer, (t_new, t_new))
            self.assertLess(backfill._captured_at(older),
                            backfill._captured_at(newer))


class ProcessOneTest(unittest.TestCase):
    """The written observation file carries the session's time, not the
    backfill run's."""

    def _fake_claude(self, payload):
        def run(prompt, model=None):
            self.prompts.append(prompt)
            return 0, json.dumps({"result": json.dumps(payload)}), ""
        return run

    def setUp(self):
        self.prompts = []
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ, {store.TARGET_STATE_DIR_ENV: str(self.root / "state")},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_written_observation_uses_transcript_mtime(self):
        path = _write_transcript(self.root / "deadbeef.jsonl")
        when = datetime(2026, 4, 17, 20, 30, 15, tzinfo=timezone.utc)
        os.utime(path, (when.timestamp(), when.timestamp()))

        # The model is told to echo captured_at back; even if it returns
        # something else, backfill overwrites it.
        payload = {
            "session_id": "ffffffff",
            "captured_at": "1999-01-01T00:00:00Z",
            "observations": [
                {
                    "dimension": "communication_style",
                    "observation": "Terse, single-line directives.",
                    "evidence": "add the retry loop",
                    "volatility": "stable",
                },
            ],
        }
        with mock.patch.object(backfill, "run_claude",
                               side_effect=self._fake_claude(payload)):
            outcome = backfill._process_one(
                path, store.load_dimensions(), model=None, force=False,
            )

        self.assertEqual(outcome, "wrote")
        written = json.loads(
            store.observation_path("deadbeef").read_text(encoding="utf-8")
        )
        self.assertEqual(written["captured_at"], "2026-04-17T20:30:15Z")
        self.assertEqual(written["session_id"], "deadbeef")
        self.assertEqual(store.validate_observation(written), [])
        # The prompt the model saw carries the same timestamp.
        self.assertIn("2026-04-17T20:30:15Z", self.prompts[0])


if __name__ == "__main__":
    unittest.main()
