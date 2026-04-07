#!/usr/bin/env python3
"""Tests for runner/validate_plan.py.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
"""
import unittest
from pathlib import Path

# validate_plan lives next to this file
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_plan import (
    validate,
    _check_test_code_spec_format,
    _check_banned_tokens,
    _check_test_failure_language,
)


def _base_plan(steps):
    """Wrap a list of steps into a minimally valid plan structure."""
    return {
        "uuid": "test-uuid",
        "executor_model": "sonnet",
        "spec": {"acceptance_criteria": []},
        "steps": steps,
    }


def _step(num, action, code_spec, description="desc", files=None):
    return {
        "step_number": num,
        "type": action,
        "description": description,
        "action": action,
        "files": files if files is not None else [],
        "depends_on": [],
        "code_spec": code_spec,
    }


class TestTestCodeSpecFormat(unittest.TestCase):
    """The validator must reject prose code_spec on test-action steps because
    the executor passes it directly to subprocess.run(shell=True). T4 step 6
    hit this exactly with 'Run python -m pytest ...'."""

    def test_single_command_passes(self):
        steps = [_step(1, "test", "python -m unittest runner.test_foo")]
        self.assertEqual(_check_test_code_spec_format(steps), [])

    def test_prose_starter_run_rejected(self):
        steps = [_step(1, "test", "Run python -m unittest runner.test_foo")]
        errors = _check_test_code_spec_format(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("prose word 'run'", errors[0])

    def test_prose_starter_execute_rejected(self):
        steps = [_step(1, "test", "Execute the test suite")]
        self.assertEqual(len(_check_test_code_spec_format(steps)), 1)

    def test_multiline_prose_rejected(self):
        steps = [_step(1, "test", "Run the unit tests:\npython -m unittest")]
        errors = _check_test_code_spec_format(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("single shell command on one line", errors[0])

    def test_non_test_action_ignored(self):
        # 'Run' would be flagged as prose, but only test actions get checked
        steps = [_step(1, "create", "Run something — this is freeform pseudocode")]
        self.assertEqual(_check_test_code_spec_format(steps), [])

    def test_empty_code_spec_skipped(self):
        # Empty is caught by the required-field check, not this one
        steps = [_step(1, "test", "")]
        self.assertEqual(_check_test_code_spec_format(steps), [])

    def test_punctuation_after_first_word_still_rejected(self):
        steps = [_step(1, "test", "Run: python -m unittest")]
        errors = _check_test_code_spec_format(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'run'", errors[0])

    def test_full_validate_surfaces_format_errors(self):
        # Confirm the new check is wired into validate()
        plan = _base_plan([
            _step(1, "create", "Make a file", files=["runner/new_file.py"]),
            _step(2, "test", "Run the tests"),
        ])
        errors = validate(plan)
        self.assertTrue(any("prose word" in e for e in errors),
                        f"expected prose-word error in {errors}")


class TestTestFailureLanguage(unittest.TestCase):
    """The validator must reject test-action steps whose description signals
    expected failure. The executor treats every test step as a hard pass/fail
    gate, so a 'this run is expected to report violations' step always aborts.
    Caught in T5b plan step 3 (#211)."""

    def test_clean_test_step_passes(self):
        steps = [_step(1, "test", "python -m unittest runner.test_foo",
                       description="Verify the new helper passes its unit tests")]
        self.assertEqual(_check_test_failure_language(steps), [])

    def test_expected_to_fail_rejected(self):
        steps = [_step(1, "test", "python audit.py",
                       description="Run the audit (expected to fail before fix)")]
        errors = _check_test_failure_language(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected to fail", errors[0])

    def test_expected_to_report_violations_rejected(self):
        steps = [_step(1, "test", "python audit.py",
                       description="Run audit (this run is expected to report violations; "
                                   "it gates subsequent fix steps)")]
        errors = _check_test_failure_language(steps)
        self.assertEqual(len(errors), 1)
        # Both phrases match — the function returns on first match.
        self.assertTrue(
            "expected to report violations" in errors[0]
            or "this run is expected to" in errors[0]
        )

    def test_should_fail_rejected(self):
        steps = [_step(1, "test", "python check.py",
                       description="The pre-fix snapshot — should fail until step 4 lands")]
        self.assertEqual(len(_check_test_failure_language(steps)), 1)

    def test_non_test_action_ignored(self):
        steps = [_step(1, "create", "make file",
                       description="this run is expected to fail before the fix")]
        self.assertEqual(_check_test_failure_language(steps), [])

    def test_full_validate_surfaces_failure_language(self):
        plan = _base_plan([
            _step(1, "create", "Add helper", files=["runner/new_file.py"]),
            _step(2, "test", "python audit.py",
                  description="Run the audit script (expected to report violations; "
                              "gates subsequent fix steps)"),
        ])
        errors = validate(plan)
        self.assertTrue(
            any("expected" in e and "test" in e for e in errors),
            f"expected failure-language error in {errors}",
        )


class TestBannedTokens(unittest.TestCase):
    """The validator must reject plans that propose pytest, shell=True, or
    external imports — all hard rule violations per docs/standards/code-style.md.
    T4 step 5 hit pytest exactly: the planner wrote a pytest test file in a
    codebase that mandates unittest stdlib only."""

    def test_clean_plan_passes(self):
        steps = [_step(1, "create", "Add a new helper function")]
        self.assertEqual(_check_banned_tokens(steps), [])

    def test_pytest_in_code_spec_rejected(self):
        steps = [_step(1, "create", "Use pytest fixtures to set up the test")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'pytest'", errors[0])
        self.assertIn("unittest", errors[0])

    def test_pytest_in_description_rejected(self):
        steps = [_step(1, "create", "freeform pseudocode here",
                       description="Add pytest tests for the new module")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)

    def test_shell_true_rejected(self):
        steps = [_step(1, "create", "Call subprocess.run(cmd, shell=True)")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("'shell=true'", errors[0])

    def test_import_requests_rejected(self):
        steps = [_step(1, "create", "import requests\nrequests.get(url)")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("stdlib only", errors[0])

    def test_from_requests_rejected(self):
        steps = [_step(1, "create", "from requests import Session")]
        self.assertEqual(len(_check_banned_tokens(steps)), 1)

    def test_case_insensitive(self):
        # Banned tokens match case-insensitively
        steps = [_step(1, "create", "Use PyTest for the suite")]
        self.assertEqual(len(_check_banned_tokens(steps)), 1)

    def test_multiple_violations_all_reported(self):
        steps = [_step(1, "create", "Use pytest with shell=True calls")]
        errors = _check_banned_tokens(steps)
        self.assertEqual(len(errors), 2)

    def test_full_validate_surfaces_banned_token_errors(self):
        # Confirm the new check is wired into validate()
        plan = _base_plan([
            _step(1, "create", "Write a pytest test file",
                  files=["runner/test_new.py"]),
        ])
        errors = validate(plan)
        self.assertTrue(any("banned token" in e for e in errors),
                        f"expected banned-token error in {errors}")


if __name__ == "__main__":
    unittest.main()
