#!/usr/bin/env python3
"""Tests for pipeline/validate_plan.py.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
"""
import unittest
from pathlib import Path

# validate_plan lives next to this file
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_plan import validate, _check_test_code_spec_format


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
        steps = [_step(1, "test", "python -m unittest pipeline.test_foo")]
        self.assertEqual(_check_test_code_spec_format(steps), [])

    def test_prose_starter_run_rejected(self):
        steps = [_step(1, "test", "Run python -m unittest pipeline.test_foo")]
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
            _step(1, "create", "Make a file", files=["pipeline/new_file.py"]),
            _step(2, "test", "Run the tests"),
        ])
        errors = validate(plan)
        self.assertTrue(any("prose word" in e for e in errors),
                        f"expected prose-word error in {errors}")


if __name__ == "__main__":
    unittest.main()
