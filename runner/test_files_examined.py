#!/usr/bin/env python3
"""Tests for files_examined field in spec validation and plan prompt injection."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runner.auto_plan import build_prompt
from runner.validate_spec import validate_files_examined

REPO_ROOT = Path(__file__).resolve().parent.parent

def _base_spec(**overrides):
    """Return a minimally valid spec dict with optional overrides."""
    spec = {
        "goal": {"problem": "X is broken", "solution": "Fix X", "value": "Users benefit"},
        "shape": {
            "components": [{"name": "c", "description": "d"}],
            "integration_point": "here",
            "pattern": "existing",
            "data_flow": "A → B",
            "dependencies": "none",
        },
        "behavior": {
            "input": "data", "processing": "steps", "output": "result",
            "error_cases": [{"trigger": "bad input", "behavior": "error shown"}],
            "edge_cases": [{"condition": "empty input", "behavior": "default used"}],
        },
        "boundaries": {
            "in_scope": ["feature"],
            "out_of_scope": [{"item": "other", "reason": "not needed"}],
            "must_not_break": ["existing"],
        },
        "acceptance_criteria": [
            "Given bad input, when submitted, then error shown",
            "Given empty input, when submitted, then default used",
        ],
        "id": "test-id",
        "intake_id": "test-id",
        "valid": True,
    }
    spec.update(overrides)
    return spec


class TestValidateFilesExamined(unittest.TestCase):
    """Unit tests for validate_files_examined()."""

    def test_absent_field_valid(self):
        self.assertEqual(validate_files_examined({}), [])

    def test_empty_array_valid(self):
        self.assertEqual(validate_files_examined({"files_examined": []}), [])

    def test_valid_entries(self):
        data = {"files_examined": [
            {"path": "runner/auto_plan.py", "sha": "abc123", "summary": "Planner logic"},
            {"path": "runner/run.py", "sha": None, "summary": "Orchestrator"},
        ]}
        self.assertEqual(validate_files_examined(data), [])

    def test_empty_path_rejected(self):
        data = {"files_examined": [{"path": "", "sha": "abc", "summary": "desc"}]}
        errors = validate_files_examined(data)
        self.assertEqual(len(errors), 1)
        self.assertIn("path", errors[0])

    def test_empty_summary_rejected(self):
        data = {"files_examined": [{"path": "a.py", "sha": "abc", "summary": ""}]}
        errors = validate_files_examined(data)
        self.assertEqual(len(errors), 1)
        self.assertIn("summary", errors[0])

    def test_invalid_sha_hex_rejected(self):
        data = {"files_examined": [{"path": "a.py", "sha": "ZZZZ", "summary": "desc"}]}
        errors = validate_files_examined(data)
        self.assertEqual(len(errors), 1)
        self.assertIn("sha", errors[0])

    def test_null_sha_valid(self):
        data = {"files_examined": [{"path": "a.py", "sha": None, "summary": "desc"}]}
        self.assertEqual(validate_files_examined(data), [])

    def test_omitted_sha_valid(self):
        data = {"files_examined": [{"path": "a.py", "summary": "desc"}]}
        self.assertEqual(validate_files_examined(data), [])

    def test_non_array_rejected(self):
        for bad_value in ("not_an_array", 42, True, {"path": "a.py"}):
            with self.subTest(value=bad_value):
                errors = validate_files_examined({"files_examined": bad_value})
                self.assertEqual(len(errors), 1)
                self.assertIn("array", errors[0])


class TestValidateSpecCLIFilesExamined(unittest.TestCase):
    """Integration tests: run validate_spec.py as subprocess with files_examined."""

    def _run_validate(self, spec_dict):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "spec.json"
            p.write_text(json.dumps(spec_dict), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "runner.validate_spec", str(p)],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )
        return result

    def test_spec_with_valid_files_examined_passes(self):
        spec = _base_spec(files_examined=[
            {"path": "runner/auto_plan.py", "sha": "abc123", "summary": "Planner logic"},
            {"path": "runner/run.py", "sha": "def456", "summary": "Orchestrator"},
            {"path": "runner/config.py", "sha": "789abc", "summary": "Config loader"},
        ])
        result = self._run_validate(spec)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Valid", result.stdout)

    def test_spec_without_files_examined_passes(self):
        spec = _base_spec()
        result = self._run_validate(spec)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Valid", result.stdout)

    def test_spec_with_empty_files_examined_passes(self):
        spec = _base_spec(files_examined=[])
        result = self._run_validate(spec)
        self.assertEqual(result.returncode, 0)

    def test_empty_path_rejected(self):
        spec = _base_spec(files_examined=[{"path": "", "sha": "abc", "summary": "desc"}])
        result = self._run_validate(spec)
        self.assertEqual(result.returncode, 1)
        self.assertIn("path", result.stderr)

    def test_empty_summary_rejected(self):
        spec = _base_spec(files_examined=[{"path": "a.py", "sha": "abc", "summary": ""}])
        result = self._run_validate(spec)
        self.assertEqual(result.returncode, 1)
        self.assertIn("summary", result.stderr)

    def test_invalid_sha_rejected(self):
        spec = _base_spec(files_examined=[{"path": "a.py", "sha": "ZZZZ", "summary": "desc"}])
        result = self._run_validate(spec)
        self.assertEqual(result.returncode, 1)
        self.assertIn("sha", result.stderr)

    def test_null_sha_passes(self):
        spec = _base_spec(files_examined=[{"path": "a.py", "sha": None, "summary": "desc"}])
        result = self._run_validate(spec)
        self.assertEqual(result.returncode, 0)


class TestAutoPlanPromptFilesExamined(unittest.TestCase):
    """Tests for build_prompt() file-trust section injection."""

    def test_prompt_includes_file_trust_section(self):
        spec = _base_spec(files_examined=[
            {"path": "runner/auto_plan.py", "sha": "abc123", "summary": "Planner logic"},
            {"path": "runner/run.py", "sha": None, "summary": "Orchestrator"},
        ])
        prompt = build_prompt(spec)
        self.assertIn("Files already examined", prompt)
        self.assertIn("runner/auto_plan.py", prompt)
        self.assertIn("Planner logic", prompt)
        self.assertIn("runner/run.py", prompt)
        self.assertIn("sha: unknown", prompt)

    def test_prompt_omits_file_trust_when_absent(self):
        spec = _base_spec()
        prompt = build_prompt(spec)
        self.assertNotIn("Files already examined", prompt)

    def test_prompt_omits_file_trust_when_empty(self):
        spec = _base_spec(files_examined=[])
        prompt = build_prompt(spec)
        self.assertNotIn("Files already examined", prompt)

    def test_prompt_deduplicates_paths(self):
        spec = _base_spec(files_examined=[
            {"path": "runner/auto_plan.py", "sha": "abc", "summary": "First read"},
            {"path": "runner/auto_plan.py", "sha": "abc", "summary": "Second read"},
        ])
        prompt = build_prompt(spec)
        count = prompt.count("`runner/auto_plan.py`")
        self.assertEqual(count, 1, f"Expected 1 occurrence, got {count}")


if __name__ == "__main__":
    unittest.main()
