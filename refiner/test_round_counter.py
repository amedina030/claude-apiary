#!/usr/bin/env python3
"""Tests for refiner/round_counter.py."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent / "round_counter.py"


def run(subcommand: str, session_id: str, tmp_dir: Path) -> str:
    """Run round_counter.py with TMP_DIR patched via env."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), subcommand, "--session-id", session_id],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "REFINER_TMP_DIR": str(tmp_dir)},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Exit {result.returncode}: {result.stderr}")
    return result.stdout.strip()


class TestRoundCounter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_start_initializes_at_zero(self):
        self.assertEqual(run("start", "s1", self.tmp_dir), "0")

    def test_tick_increments(self):
        run("start", "s1", self.tmp_dir)
        self.assertEqual(run("tick", "s1", self.tmp_dir), "1")
        self.assertEqual(run("tick", "s1", self.tmp_dir), "2")
        self.assertEqual(run("tick", "s1", self.tmp_dir), "3")

    def test_status_does_not_change_count(self):
        run("start", "s1", self.tmp_dir)
        run("tick", "s1", self.tmp_dir)
        run("tick", "s1", self.tmp_dir)
        self.assertEqual(run("status", "s1", self.tmp_dir), "2")
        self.assertEqual(run("status", "s1", self.tmp_dir), "2")

    def test_reset_sets_to_zero(self):
        run("start", "s1", self.tmp_dir)
        run("tick", "s1", self.tmp_dir)
        run("tick", "s1", self.tmp_dir)
        self.assertEqual(run("reset", "s1", self.tmp_dir), "0")
        self.assertEqual(run("status", "s1", self.tmp_dir), "0")

    def test_tick_on_unknown_session_returns_one(self):
        self.assertEqual(run("tick", "unknown", self.tmp_dir), "1")

    def test_status_on_unknown_session_returns_zero(self):
        self.assertEqual(run("status", "unknown", self.tmp_dir), "0")

    def test_start_resets_existing_session(self):
        run("start", "s1", self.tmp_dir)
        run("tick", "s1", self.tmp_dir)
        run("tick", "s1", self.tmp_dir)
        self.assertEqual(run("start", "s1", self.tmp_dir), "0")
        self.assertEqual(run("status", "s1", self.tmp_dir), "0")

    def test_sessions_are_isolated(self):
        run("start", "a", self.tmp_dir)
        run("start", "b", self.tmp_dir)
        run("tick", "a", self.tmp_dir)
        run("tick", "a", self.tmp_dir)
        run("tick", "b", self.tmp_dir)
        self.assertEqual(run("status", "a", self.tmp_dir), "2")
        self.assertEqual(run("status", "b", self.tmp_dir), "1")

    def test_state_file_is_valid_json(self):
        run("start", "s1", self.tmp_dir)
        run("tick", "s1", self.tmp_dir)
        state_file = self.tmp_dir / "round_s1.json"
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["session_id"], "s1")
        self.assertEqual(data["count"], 1)


if __name__ == "__main__":
    unittest.main()
