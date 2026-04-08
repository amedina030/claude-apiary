#!/usr/bin/env python3
"""Tests for core/hooks/context_rule_error_reminder.py.

Imports the hook module directly and exercises _looks_like_failure for the
detection logic, plus runs the hook as a subprocess for end-to-end stdin
payload handling and exact injected text.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "context_rule_error_reminder.py"
PYTHON = sys.executable

# Ensure the hook module is importable for direct unit tests of helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import context_rule_error_reminder as hook_mod  # noqa: E402


def _run_hook(payload: dict) -> tuple[str, int]:
    proc = subprocess.run(
        [PYTHON, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=10,
    )
    return proc.stdout, proc.returncode


def _additional_context(stdout: str) -> str:
    if not stdout.strip():
        return ""
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    return (obj.get("hookSpecificOutput") or {}).get("additionalContext", "")


class TestFailureDetection(unittest.TestCase):
    def test_interrupted_flag(self):
        self.assertTrue(hook_mod._looks_like_failure({"interrupted": True}))

    def test_is_error_flag(self):
        self.assertTrue(hook_mod._looks_like_failure({"is_error": True}))
        self.assertTrue(hook_mod._looks_like_failure({"isError": True}))

    def test_nonzero_exit_codes(self):
        self.assertTrue(hook_mod._looks_like_failure({"exit_code": 1}))
        self.assertTrue(hook_mod._looks_like_failure({"exitCode": 2}))
        self.assertTrue(hook_mod._looks_like_failure({"returncode": 127}))

    def test_zero_exit_code_is_success(self):
        self.assertFalse(hook_mod._looks_like_failure({"exit_code": 0, "stdout": "ok"}))

    def test_traceback_in_stderr(self):
        resp = {"stderr": "Traceback (most recent call last):\n  File ..."}
        self.assertTrue(hook_mod._looks_like_failure(resp))

    def test_string_response_with_error(self):
        self.assertTrue(hook_mod._looks_like_failure("Error: command not found"))

    def test_empty_string_response(self):
        self.assertFalse(hook_mod._looks_like_failure(""))

    def test_hook_denial_string_excluded(self):
        # Hook denials look "error-like" but the rule does not apply.
        s = "Hook PreToolUse:Bash denied this tool"
        self.assertFalse(hook_mod._looks_like_failure(s))

    def test_hook_denial_dict_excluded(self):
        resp = {"stderr": "Blocked by some_hook: ..."}
        self.assertFalse(hook_mod._looks_like_failure(resp))

    def test_plain_success(self):
        self.assertFalse(hook_mod._looks_like_failure({"stdout": "hello\n", "stderr": ""}))

    def test_non_dict_non_string(self):
        self.assertFalse(hook_mod._looks_like_failure(None))
        self.assertFalse(hook_mod._looks_like_failure(42))


class TestHookSubprocess(unittest.TestCase):
    def test_failing_bash_injects_both_reminders(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"stdout": "", "stderr": "", "exit_code": 1},
        }
        stdout, rc = _run_hook(payload)
        self.assertEqual(rc, 0)
        ctx = _additional_context(stdout)
        self.assertIn("recover_from_trivial_errors", ctx)
        self.assertIn("Errors Signal Doc Gaps", ctx)

    def test_succeeding_bash_no_reminder(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo ok"},
            "tool_response": {"stdout": "ok\n", "stderr": "", "exit_code": 0},
        }
        stdout, rc = _run_hook(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(_additional_context(stdout), "")

    def test_hook_denial_no_reminder(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "python scribe/notes.py list"},
            "tool_response": "Hook PreToolUse:Bash denied this tool",
        }
        stdout, rc = _run_hook(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(_additional_context(stdout), "")

    def test_non_bash_tool_skipped(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/nope"},
            "tool_response": {"is_error": True, "stderr": "ENOENT"},
        }
        stdout, rc = _run_hook(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(_additional_context(stdout), "")

    def test_missing_tool_response_no_crash(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        stdout, rc = _run_hook(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(_additional_context(stdout), "")

    def test_empty_stdin_no_crash(self):
        proc = subprocess.run(
            [PYTHON, str(HOOK)],
            input="",
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_malformed_json_no_crash(self):
        proc = subprocess.run(
            [PYTHON, str(HOOK)],
            input="{not json",
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_traceback_in_stdout_triggers(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "python -c 'raise ValueError'"},
            "tool_response": {
                "stdout": "",
                "stderr": "Traceback (most recent call last):\n  File ...\nValueError",
                "exit_code": 1,
            },
        }
        stdout, rc = _run_hook(payload)
        self.assertEqual(rc, 0)
        ctx = _additional_context(stdout)
        self.assertIn("recover_from_trivial_errors", ctx)


if __name__ == "__main__":
    unittest.main()
