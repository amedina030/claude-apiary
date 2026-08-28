#!/usr/bin/env python3
"""Tests for harden/round_counter.py."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "round_counter.py")
PYTHON = sys.executable


def run(
    subcommand: str, session_id: str, extra_args: list = None, tmp_dir: str = None
) -> subprocess.CompletedProcess:
    cmd = [PYTHON, SCRIPT, subcommand, "--session-id", session_id]
    if extra_args:
        cmd.extend(extra_args)
    env = None
    if tmp_dir:
        import os

        env = {**os.environ, "HARDEN_TMP_DIR": tmp_dir}
    return subprocess.run(cmd, text=True, capture_output=True, env=env)


class TestRoundCounter(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def run_cmd(self, subcommand, session_id="test", extra_args=None):
        return run(subcommand, session_id, extra_args, self.tmp)

    def read_state(self, session_id="test"):
        path = Path(self.tmp) / f"round_{session_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_start_initializes_zero(self):
        r = self.run_cmd("start")
        self.assertEqual(r.stdout.strip(), "0")
        state = self.read_state()
        self.assertEqual(state["count"], 0)
        self.assertEqual(state["session_id"], "test")

    def test_tick_increments(self):
        self.run_cmd("start")
        r = self.run_cmd("tick")
        self.assertEqual(r.stdout.strip(), "1")
        r = self.run_cmd("tick")
        self.assertEqual(r.stdout.strip(), "2")

    def test_reset_clears_count(self):
        self.run_cmd("start")
        self.run_cmd("tick")
        r = self.run_cmd("reset")
        self.assertEqual(r.stdout.strip(), "0")

    def test_status_reads_without_changing(self):
        self.run_cmd("start")
        self.run_cmd("tick")
        r = self.run_cmd("status")
        self.assertEqual(r.stdout.strip(), "1")
        r = self.run_cmd("status")
        self.assertEqual(r.stdout.strip(), "1")


class TestDefenderSubcommand(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def run_cmd(self, subcommand, session_id="test", extra_args=None):
        return run(subcommand, session_id, extra_args, self.tmp)

    def read_state(self, session_id="test"):
        path = Path(self.tmp) / f"round_{session_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_set_stores_and_prints_id(self):
        self.run_cmd("start")
        r = self.run_cmd("defender", extra_args=["--set", "agent-xyz"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "agent-xyz")
        state = self.read_state()
        self.assertEqual(state["defender_agent_id"], "agent-xyz")

    def test_get_retrieves_stored_id(self):
        self.run_cmd("start")
        self.run_cmd("defender", extra_args=["--set", "agent-abc"])
        r = self.run_cmd("defender", extra_args=["--get"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "agent-abc")

    def test_get_fails_when_no_id(self):
        self.run_cmd("start")
        r = self.run_cmd("defender", extra_args=["--get"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("no defender_agent_id", r.stderr)

    def test_tick_preserves_defender_id(self):
        self.run_cmd("start")
        self.run_cmd("defender", extra_args=["--set", "agent-123"])
        self.run_cmd("tick")
        state = self.read_state()
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["defender_agent_id"], "agent-123")

    def test_reset_clears_defender_id(self):
        self.run_cmd("start")
        self.run_cmd("defender", extra_args=["--set", "agent-123"])
        self.run_cmd("reset")
        r = self.run_cmd("defender", extra_args=["--get"])
        self.assertEqual(r.returncode, 1)

    def test_set_overwrites_existing_id(self):
        self.run_cmd("start")
        self.run_cmd("defender", extra_args=["--set", "old-id"])
        self.run_cmd("defender", extra_args=["--set", "new-id"])
        r = self.run_cmd("defender", extra_args=["--get"])
        self.assertEqual(r.stdout.strip(), "new-id")

    def test_set_and_get_require_mutually_exclusive(self):
        self.run_cmd("start")
        r = self.run_cmd("defender", extra_args=["--set", "x", "--get"])
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
