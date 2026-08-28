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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.testing import hermetic_env  # noqa: E402

HOOKS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
SAMPLE_SID = "abcd1234-1111-2222-3333-444444444444"


def _run_hook(
    hook_name: str, payload: dict, home: Path, *, runner_subprocess: bool
) -> subprocess.CompletedProcess:
    # Per-repo session state (review S1): flags under <repo>/.claude/apiary/
    # session-tmp, identity/history under <state-dir>/sessions. Point both at
    # temp dirs so the hook never touches this checkout's own state.
    repo = home / "repo"
    (repo / ".claude" / "apiary" / "session-tmp").mkdir(parents=True, exist_ok=True)
    (repo / ".claude" / "apiary" / "self-pointer.json").write_text(
        json.dumps({"schema_version": 1, "name": "repo", "uid": 1, "real_path": str(repo)}),
        encoding="utf-8",
    )
    # hermetic_env drops every inherited APIARY_* / CLAUDE_PROJECT_DIR before
    # the overrides go on, so a live session cannot point the hook at the real
    # checkout.
    env = hermetic_env(
        HOME=str(home),
        USERPROFILE=str(home),
        APIARY_TARGET_REPO=str(repo),
        APIARY_TARGET_STATE_DIR=str(home / "state"),
        APIARY_RUNNER_SUBPROCESS="1" if runner_subprocess else "",
    )
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


class TestInjectSessionGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()

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
        self.home = Path(self._tmp.name).resolve()

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
