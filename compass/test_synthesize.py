"""Tests for compass/synthesize.py — observation cap and atomic write.

Hermetic: ``APIARY_TARGET_STATE_DIR`` points at a tempdir for every test,
so the live compass store and the operator's real personality.md are never
read or written, and the ``claude`` subprocess is always mocked.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from compass import store, synthesize


def _observation(sid: str, captured_at: str) -> dict:
    return {
        "session_id": sid,
        "captured_at": captured_at,
        "observations": [
            {
                "dimension": "communication_style",
                "observation": f"Session {sid} was terse.",
                "evidence": f"evidence-{sid}",
                "volatility": "stable",
            },
        ],
    }


class CapSessionsTest(unittest.TestCase):
    def _obs(self, n):
        return [_observation(f"{i:08x}", f"2026-0{i}-01T00:00:00Z") for i in range(1, n + 1)]

    def test_keeps_the_head_of_a_newest_first_list(self):
        observations = self._obs(5)
        capped = synthesize._cap_sessions(observations, 2)
        self.assertEqual(
            [o["session_id"] for o in capped],
            [observations[0]["session_id"], observations[1]["session_id"]],
        )

    def test_no_cap_when_under_the_limit(self):
        observations = self._obs(3)
        self.assertEqual(synthesize._cap_sessions(observations, 50), observations)

    def test_zero_disables_the_cap(self):
        observations = self._obs(3)
        self.assertEqual(synthesize._cap_sessions(observations, 0), observations)

    def test_negative_disables_the_cap(self):
        observations = self._obs(3)
        self.assertEqual(synthesize._cap_sessions(observations, -1), observations)

    def test_default_matches_the_archive_threshold(self):
        # The cap and the point where archiving starts should not drift apart.
        self.assertEqual(synthesize.DEFAULT_MAX_SESSIONS, store.ARCHIVE_MIN_ACTIVE)


class SynthesizeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name).resolve() / "state"
        self._env = mock.patch.dict(
            os.environ,
            {store.TARGET_STATE_DIR_ENV: str(self.state)},
        )
        self._env.start()
        store.ensure_layout()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def write_observations(self, count):
        """Write *count* observation files, oldest first by captured_at."""
        for i in range(1, count + 1):
            sid = f"{i:08x}"
            payload = _observation(sid, f"2026-{i:02d}-01T00:00:00Z")
            store.observation_path(sid).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

    def run_main(self, argv, claude=None):
        """Run synthesize.main() with argv; returns (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        patches = [
            mock.patch.object(sys, "argv", ["synthesize.py"] + argv),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ]
        if claude is not None:
            patches.append(mock.patch.object(synthesize, "run_claude", side_effect=claude))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            rc = synthesize.main()
        return rc, out.getvalue(), err.getvalue()


class ObservationCapTest(SynthesizeTestBase):
    def test_prompt_carries_only_the_most_recent_sessions(self):
        self.write_observations(4)
        rc, out, err = self.run_main(["--dry-run", "--max-sessions", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("2 session(s), newest first", out)
        # 04 and 03 are the two newest captured_at values.
        self.assertIn("evidence-00000004", out)
        self.assertIn("evidence-00000003", out)
        self.assertNotIn("evidence-00000002", out)
        self.assertNotIn("evidence-00000001", out)
        self.assertIn("capping synthesis at the 2 most recent session(s) of 4", err)

    def test_no_cap_message_when_nothing_is_dropped(self):
        self.write_observations(2)
        rc, out, err = self.run_main(["--dry-run", "--max-sessions", "50"])
        self.assertEqual(rc, 0)
        self.assertNotIn("capping synthesis", err)
        self.assertIn("2 session(s), newest first", out)

    def test_zero_sends_everything(self):
        self.write_observations(4)
        rc, out, err = self.run_main(["--dry-run", "--max-sessions", "0"])
        self.assertEqual(rc, 0)
        self.assertIn("4 session(s), newest first", out)
        self.assertIn("evidence-00000001", out)

    def test_load_active_sorts_newest_first(self):
        # The cap slices the head, so the sort is load-bearing.
        self.write_observations(3)
        loaded = synthesize._load_active(store.list_active_observations())
        self.assertEqual(
            [o["captured_at"] for o in loaded],
            ["2026-03-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        )


class AtomicWriteTest(SynthesizeTestBase):
    def _claude(self, text):
        def run(prompt, model=None):
            return 0, json.dumps({"result": text}), ""

        return run

    def test_writes_the_profile(self):
        self.write_observations(1)
        rc, out, _ = self.run_main([], claude=self._claude("# Personality profile\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(
            store.personality_path().read_text(encoding="utf-8"),
            "# Personality profile",
        )
        self.assertIn("wrote", out)

    def test_failed_write_leaves_the_previous_profile_intact(self):
        self.write_observations(1)
        store.personality_path().write_text("# previous profile\n", encoding="utf-8")
        with mock.patch("core.utils.atomic.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_main([], claude=self._claude("# replacement"))
        self.assertEqual(
            store.personality_path().read_text(encoding="utf-8"), "# previous profile\n"
        )
        orphans = list(store.compass_dir().glob("personality.md.*"))
        self.assertEqual(orphans, [], f"temp file left behind: {orphans}")

    def test_empty_model_output_leaves_the_previous_profile_intact(self):
        self.write_observations(1)
        store.personality_path().write_text("# previous profile\n", encoding="utf-8")
        rc, _, err = self.run_main([], claude=self._claude("   "))
        self.assertEqual(rc, 2)
        self.assertEqual(
            store.personality_path().read_text(encoding="utf-8"), "# previous profile\n"
        )
        self.assertIn("untouched", err)

    def test_claude_failure_leaves_the_previous_profile_intact(self):
        self.write_observations(1)
        store.personality_path().write_text("# previous profile\n", encoding="utf-8")

        def failing(prompt, model=None):
            return 1, "", "boom"

        rc, _, err = self.run_main([], claude=failing)
        self.assertEqual(rc, 2)
        self.assertEqual(
            store.personality_path().read_text(encoding="utf-8"), "# previous profile\n"
        )

    def test_no_active_observations_is_a_no_op(self):
        rc, _, err = self.run_main([])
        self.assertEqual(rc, 1)
        self.assertIn("no active observations", err)
        self.assertFalse(store.personality_path().exists())


if __name__ == "__main__":
    unittest.main()
