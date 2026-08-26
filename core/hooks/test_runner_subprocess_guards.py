#!/usr/bin/env python3
"""Tests that core/ hooks short-circuit on APIARY_RUNNER_SUBPROCESS=1 (#228).

For each guarded hook, runs it as a subprocess with HOME pointed at a
temp directory and the env var set, and asserts no side effects:
  - No flag files written under <repo>/.claude/apiary/session-tmp/.
  - No context block / additional output emitted.

These tests complement test_save_transcript.py, which covers the
save_transcript hook's own subprocess guard from #223.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
SAMPLE_SID = "abcd1234-1111-2222-3333-444444444444"


def _run_hook(hook_name: str, payload: dict, home: Path, *, runner_subprocess: bool) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # Per-repo session state (review S1): flags under <repo>/.claude/apiary/
    # session-tmp, identity/history under <state-dir>/sessions. Point both at
    # temp dirs so the hook never touches this checkout's own state.
    repo = home / "repo"
    (repo / ".claude" / "apiary" / "session-tmp").mkdir(parents=True, exist_ok=True)
    env["APIARY_TARGET_REPO"] = str(repo)
    env["APIARY_TARGET_STATE_DIR"] = str(home / "state")
    env.pop("CLAUDE_PROJECT_DIR", None)
    if runner_subprocess:
        env["APIARY_RUNNER_SUBPROCESS"] = "1"
    else:
        env.pop("APIARY_RUNNER_SUBPROCESS", None)
    return subprocess.run(
        [PYTHON, str(HOOKS_DIR / hook_name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )


def _flag_files(home: Path) -> list[Path]:
    """Return all session flag files written under the temp repo's session-tmp/."""
    tmp = home / "repo" / ".claude" / "apiary" / "session-tmp"
    if not tmp.exists():
        return []
    return [p for p in tmp.iterdir() if p.is_file()]


class TestCheckInstallGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_runner_subprocess_skips_check_and_writes_no_flag(self):
        result = _run_hook(
            "check_install.py",
            {"session_id": SAMPLE_SID},
            self.home,
            runner_subprocess=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(_flag_files(self.home), [])

    def test_normal_session_runs(self):
        # Sanity: with the env var unset, the hook does its work and the
        # flag file gets written. (No manifest exists, so the only side
        # effect we can observe is the flag file.)
        result = _run_hook(
            "check_install.py",
            {"session_id": SAMPLE_SID},
            self.home,
            runner_subprocess=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotEqual(_flag_files(self.home), [])


class TestInjectSessionGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_runner_subprocess_skips_injection_and_writes_no_flag(self):
        result = _run_hook(
            "inject_session.py",
            {"session_id": SAMPLE_SID},
            self.home,
            runner_subprocess=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(_flag_files(self.home), [])
        # The hook also should not have emitted a [session] context block.
        self.assertNotIn("session_id:", result.stdout)

    def test_normal_session_injects(self):
        result = _run_hook(
            "inject_session.py",
            {"session_id": SAMPLE_SID},
            self.home,
            runner_subprocess=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotEqual(_flag_files(self.home), [])
        self.assertIn("session_id", result.stdout)


class TestContextRuleErrorReminderGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _failure_payload(self) -> dict:
        return {
            "tool_name": "Bash",
            "tool_response": {
                "is_error": True,
                "stderr": "boom",
            },
        }

    def test_runner_subprocess_emits_no_reminder(self):
        result = _run_hook(
            "context_rule_error_reminder.py",
            self._failure_payload(),
            self.home,
            runner_subprocess=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn("recover_from_trivial_errors", result.stdout)
        self.assertNotIn("Errors Signal Doc Gaps", result.stdout)

    def test_normal_session_emits_reminder_on_failure(self):
        result = _run_hook(
            "context_rule_error_reminder.py",
            self._failure_payload(),
            self.home,
            runner_subprocess=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("recover_from_trivial_errors", result.stdout)
        self.assertIn("Errors Signal Doc Gaps", result.stdout)


if __name__ == "__main__":
    unittest.main()
