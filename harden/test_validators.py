#!/usr/bin/env python3
"""Tests for harden/validate_findings.py and harden/validate_response.py."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

FINDINGS_SCRIPT = str(Path(__file__).parent / "validate_findings.py")
RESPONSE_SCRIPT = str(Path(__file__).parent / "validate_response.py")
PYTHON = sys.executable


def run_validate_findings(input_json: str, check_files: bool = False, deep: bool = False) -> subprocess.CompletedProcess:
    cmd = [PYTHON, FINDINGS_SCRIPT]
    if check_files:
        cmd.append("--check-files")
    if deep:
        cmd.append("--deep")
    return subprocess.run(cmd, input=input_json, text=True, capture_output=True)


def run_validate_response(input_json: str, expected_ids: str, check_files: bool = False) -> subprocess.CompletedProcess:
    cmd = [PYTHON, RESPONSE_SCRIPT, "--expected-ids", expected_ids]
    if check_files:
        cmd.append("--check-files")
    return subprocess.run(cmd, input=input_json, text=True, capture_output=True)


def make_finding(**overrides):
    base = {
        "id": "ATK-001",
        "category": "security",
        "severity": "high",
        "description": "SQL injection in query builder",
        "location": "app/db.py:45-50",
    }
    base.update(overrides)
    return base


def make_response(**overrides):
    base = {
        "responses": [
            {
                "id": "DEF-001",
                "finding_ref": "ATK-001",
                "action": "fixed",
                "description": "Parameterized the query",
                "changes": [{"file": "app/db.py", "description": "Used parameterized queries"}],
            }
        ],
        "todos": [],
    }
    base.update(overrides)
    return base


class TestValidateFindings(unittest.TestCase):

    def test_valid_finding_passes(self):
        result = run_validate_findings(json.dumps([make_finding()]))
        self.assertEqual(result.returncode, 0)

    def test_empty_array_passes(self):
        result = run_validate_findings("[]")
        self.assertEqual(result.returncode, 0)

    def test_missing_required_field_fails(self):
        finding = make_finding()
        del finding["description"]
        result = run_validate_findings(json.dumps([finding]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing required field 'description'", result.stderr)

    def test_empty_field_fails(self):
        finding = make_finding(description="")
        result = run_validate_findings(json.dumps([finding]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("'description' is empty", result.stderr)

    def test_invalid_severity_fails(self):
        finding = make_finding(severity="extreme")
        result = run_validate_findings(json.dumps([finding]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid severity", result.stderr)

    def test_invalid_category_fails(self):
        finding = make_finding(category="performance")
        result = run_validate_findings(json.dumps([finding]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid category", result.stderr)

    def test_valid_categories(self):
        for cat in ("general", "security", "input", "logic", "complexity", "resilience"):
            finding = make_finding(category=cat)
            result = run_validate_findings(json.dumps([finding]))
            self.assertEqual(result.returncode, 0, f"Category '{cat}' should be valid")

    def test_valid_severities(self):
        for sev in ("critical", "high", "medium", "low"):
            finding = make_finding(severity=sev)
            result = run_validate_findings(json.dumps([finding]))
            self.assertEqual(result.returncode, 0, f"Severity '{sev}' should be valid")

    def test_deep_mode_requires_scenario(self):
        finding = make_finding()  # no scenario field
        result = run_validate_findings(json.dumps([finding]), deep=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--deep requires a 'scenario' field", result.stderr)

    def test_deep_mode_requires_given_when_then(self):
        finding = make_finding(scenario="The user submits bad input")
        result = run_validate_findings(json.dumps([finding]), deep=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("scenario missing", result.stderr)

    def test_deep_mode_valid_scenario(self):
        finding = make_finding(
            scenario="Given a user with admin role, When they submit a SQL payload in the name field, Then the query executes unparameterized"
        )
        result = run_validate_findings(json.dumps([finding]), deep=True)
        self.assertEqual(result.returncode, 0)

    def test_check_files_with_missing_file(self):
        finding = make_finding(location="/nonexistent/file.py:10-20")
        result = run_validate_findings(json.dumps([finding]), check_files=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("file not found", result.stderr)

    def test_check_files_with_existing_file(self):
        # Use this test file itself as the target
        finding = make_finding(location=f"{__file__}:1-10")
        result = run_validate_findings(json.dumps([finding]), check_files=True)
        self.assertEqual(result.returncode, 0)

    def test_invalid_json_fails(self):
        result = run_validate_findings("not json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid JSON", result.stderr)

    def test_empty_input_fails(self):
        result = run_validate_findings("")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Empty input", result.stderr)

    def test_multiple_errors_reported(self):
        findings = [
            make_finding(severity="extreme", description=""),
        ]
        result = run_validate_findings(json.dumps(findings))
        self.assertEqual(result.returncode, 1)
        # Should report both errors
        self.assertIn("2 errors", result.stderr)


class TestValidateResponse(unittest.TestCase):

    def test_valid_response_passes(self):
        result = run_validate_response(json.dumps(make_response()), "ATK-001")
        self.assertEqual(result.returncode, 0)

    def test_missing_finding_ref_fails(self):
        data = make_response()
        data["responses"][0]["finding_ref"] = "ATK-002"  # Wrong ref
        result = run_validate_response(json.dumps(data), "ATK-001")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ATK-001 not addressed", result.stderr)

    def test_multiple_expected_ids(self):
        data = make_response()
        # Only addresses ATK-001, missing ATK-002
        result = run_validate_response(json.dumps(data), "ATK-001,ATK-002")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ATK-002 not addressed", result.stderr)

    def test_all_expected_ids_addressed(self):
        data = {
            "responses": [
                {"finding_ref": "ATK-001", "action": "fixed", "description": "Fixed it", "changes": []},
                {"finding_ref": "ATK-002", "action": "deferred", "description": "Deferred", "changes": []},
            ],
            "todos": [],
        }
        result = run_validate_response(json.dumps(data), "ATK-001,ATK-002")
        self.assertEqual(result.returncode, 0)

    def test_invalid_action_fails(self):
        data = make_response()
        data["responses"][0]["action"] = "ignored"
        result = run_validate_response(json.dumps(data), "ATK-001")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid action", result.stderr)

    def test_valid_actions(self):
        for action in ("fixed", "refactored", "deferred"):
            data = make_response()
            data["responses"][0]["action"] = action
            result = run_validate_response(json.dumps(data), "ATK-001")
            self.assertEqual(result.returncode, 0, f"Action '{action}' should be valid")

    def test_empty_description_fails(self):
        data = make_response()
        data["responses"][0]["description"] = ""
        result = run_validate_response(json.dumps(data), "ATK-001")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'description' is empty", result.stderr)

    def test_missing_responses_field_fails(self):
        result = run_validate_response('{"todos": []}', "ATK-001")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing 'responses'", result.stderr)

    def test_empty_todo_content_fails(self):
        data = make_response()
        data["todos"] = [{"content": ""}]
        result = run_validate_response(json.dumps(data), "ATK-001")
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty 'content'", result.stderr)

    def test_valid_todos(self):
        data = make_response()
        data["todos"] = [{"content": "Refactor the error handling in utils.py"}]
        result = run_validate_response(json.dumps(data), "ATK-001")
        self.assertEqual(result.returncode, 0)

    def test_invalid_json_fails(self):
        result = run_validate_response("not json", "ATK-001")
        self.assertEqual(result.returncode, 1)

    def test_empty_input_fails(self):
        result = run_validate_response("", "ATK-001")
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
