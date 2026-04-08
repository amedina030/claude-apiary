#!/usr/bin/env python3
"""Tests for runner/validate_plan.py.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# validate_plan lives next to this file
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_plan import (
    validate,
    _check_test_code_spec_format,
    _check_banned_tokens,
    _check_test_failure_language,
    _check_path_allowlist,
)
import validate_plan


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


class TestPathAllowlist(unittest.TestCase):
    """#212: absolute paths in step.files must resolve under one of the
    allowlist roots (repo root, ~/.claude/projects/<project-key>/) or
    be flat-out rejected. Catches T5b's accidental Windows paths
    without blocking the legitimate #222 case of writing persistent
    state under ~/.claude/projects/."""

    def setUp(self):
        # Force a known set of allowlist roots so the tests are
        # deterministic regardless of the host's home directory layout.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_root = Path(self._tmp.name)
        self.fake_state_root = tmp_root / "state"
        self.fake_state_root.mkdir(parents=True)
        # A definitely-absolute path that lives nowhere near the
        # fake_state_root or the repo root — used as the "rejected"
        # case so we don't depend on /tmp existing or being absolute
        # on Windows.
        self.outside_root = tmp_root / "outside"
        self.outside_root.mkdir(parents=True)
        self._allow_patch = mock.patch.object(
            validate_plan,
            "_allowlist_roots",
            return_value=[
                validate_plan._REPO_ROOT.resolve(),
                self.fake_state_root.resolve(),
            ],
        )
        self._allow_patch.start()
        self.addCleanup(self._allow_patch.stop)

    def test_relative_path_accepted(self):
        steps = [_step(1, "create", "x", files=["runner/foo.py"])]
        self.assertEqual(_check_path_allowlist(steps), [])

    def test_absolute_path_under_repo_accepted(self):
        in_repo = validate_plan._REPO_ROOT / "runner" / "foo.py"
        steps = [_step(1, "create", "x", files=[str(in_repo)])]
        self.assertEqual(_check_path_allowlist(steps), [])

    def test_absolute_path_under_state_dir_accepted(self):
        outside = self.fake_state_root / "subdir" / "backfill_skip.json"
        steps = [_step(1, "create", "x", files=[str(outside)])]
        self.assertEqual(_check_path_allowlist(steps), [])

    def test_absolute_path_outside_allowlist_rejected(self):
        bad = str(self.outside_root / "random.json")
        steps = [_step(1, "create", "x", files=[bad])]
        errors = _check_path_allowlist(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("outside the allowlist", errors[0])
        self.assertIn(bad, errors[0])

    def test_windows_style_absolute_path_rejected(self):
        # T5b regression: planner emitted C:\Users\... paths in files[].
        steps = [_step(1, "create", "x", files=["C:\\Users\\amedi\\.claude\\CLAUDE.md"])]
        errors = _check_path_allowlist(steps)
        # On non-Windows, "C:\\..." may be parsed as relative; either accepted
        # (relative) or rejected, but never accepted *as* an out-of-allowlist
        # absolute. Skip the strict check on POSIX where the parser is lenient.
        if Path("C:\\Users\\amedi\\.claude\\CLAUDE.md").is_absolute():
            self.assertEqual(len(errors), 1)
            self.assertIn("outside the allowlist", errors[0])

    def test_mixed_files_in_one_step(self):
        ok_in_repo = str(validate_plan._REPO_ROOT / "runner" / "foo.py")
        ok_state = str(self.fake_state_root / "state.json")
        bad = str(self.outside_root / "path.json")
        steps = [_step(1, "create", "x", files=[ok_in_repo, ok_state, bad])]
        errors = _check_path_allowlist(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn(bad, errors[0])

    def test_full_validate_surfaces_allowlist_errors(self):
        bad = str(self.outside_root / "path.json")
        plan = _base_plan([
            _step(1, "create", "noop spec", files=[bad]),
        ])
        errors = validate(plan)
        self.assertTrue(any("outside the allowlist" in e for e in errors),
                        f"expected allowlist error in {errors}")


if __name__ == "__main__":
    unittest.main()
