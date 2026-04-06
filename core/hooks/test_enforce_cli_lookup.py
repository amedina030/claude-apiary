#!/usr/bin/env python3
"""Tests for core/hooks/enforce_cli_lookup.py.

Builds a fake transcript under a temp HOME, invokes the hook as a subprocess
with various PreToolUse payloads, and asserts on exit codes and stderr.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "enforce_cli_lookup.py"
PYTHON = sys.executable


def _project_key(cwd: str) -> str:
    return "".join("-" if c in (":", "/", "\\") else c for c in cwd)


def _tool_use(command: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            ]
        },
    }


def _write_transcript(home: Path, cwd: str, session_id: str, lines: list[dict]) -> Path:
    project_dir = home / ".claude" / "projects" / _project_key(cwd)
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _run_hook(payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return subprocess.run(
        [PYTHON, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )


class EnforceCliLookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cwd = "D:\\Professional\\claude-apiary"
        self.session_id = "test-session-0001"

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, command: str) -> dict:
        return {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": self.session_id,
            "cwd": self.cwd,
        }

    def test_blocks_repo_tool_without_prior_lookup(self):
        _write_transcript(self.home, self.cwd, self.session_id, [])
        result = _run_hook(self._payload("python scribe/notes.py list"), self.home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("notes.py", result.stderr)

    def test_allows_repo_tool_after_prior_lookup(self):
        _write_transcript(
            self.home,
            self.cwd,
            self.session_id,
            [_tool_use("python docs/reference/cli_lookup.py notes.py")],
        )
        result = _run_hook(self._payload("python scribe/notes.py list"), self.home)
        self.assertEqual(result.returncode, 0)

    def test_allows_cli_lookup_itself(self):
        _write_transcript(self.home, self.cwd, self.session_id, [])
        result = _run_hook(
            self._payload("python docs/reference/cli_lookup.py notes.py"), self.home
        )
        self.assertEqual(result.returncode, 0)

    def test_allows_unrelated_command(self):
        _write_transcript(self.home, self.cwd, self.session_id, [])
        result = _run_hook(self._payload("ls -la"), self.home)
        self.assertEqual(result.returncode, 0)

    def test_blocks_chained_command_when_first_is_unlookedup_repo_tool(self):
        """Regression: a && b && c — if any subcommand is a repo CLI tool
        without a prior lookup, the whole call must be blocked."""
        _write_transcript(self.home, self.cwd, self.session_id, [])
        cmd = 'python scribe/notes.py list --type context && echo "---" && ls'
        result = _run_hook(self._payload(cmd), self.home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("notes.py", result.stderr)

    def test_blocks_chained_command_when_later_subcmd_is_unlookedup(self):
        """Regression: lookup covers tool A, but chained call also invokes
        tool B which was never looked up — must block on B."""
        _write_transcript(
            self.home,
            self.cwd,
            self.session_id,
            [_tool_use("python docs/reference/cli_lookup.py notes.py")],
        )
        cmd = "python scribe/notes.py list && python budgeter/report.py"
        result = _run_hook(self._payload(cmd), self.home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("report.py", result.stderr)

    def test_allows_chained_command_when_all_tools_looked_up(self):
        _write_transcript(
            self.home,
            self.cwd,
            self.session_id,
            [
                _tool_use("python docs/reference/cli_lookup.py notes.py"),
                _tool_use("python docs/reference/cli_lookup.py report.py"),
            ],
        )
        cmd = "python scribe/notes.py list && python budgeter/report.py"
        result = _run_hook(self._payload(cmd), self.home)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
