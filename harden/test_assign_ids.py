#!/usr/bin/env python3
"""Tests for harden/assign_ids.py."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "assign_ids.py")
PYTHON = sys.executable


def run_assign(input_json: str, prefix: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, SCRIPT, "--prefix", prefix],
        input=input_json,
        text=True,
        capture_output=True,
    )


class TestAssignIds(unittest.TestCase):

    def test_atk_prefix_assigns_sequential_ids(self):
        items = [{"category": "security"}, {"category": "input"}]
        result = run_assign(json.dumps(items), "ATK")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0]["id"], "ATK-001")
        self.assertEqual(output[1]["id"], "ATK-002")

    def test_def_prefix_assigns_sequential_ids(self):
        items = [{"finding_ref": "ATK-001"}, {"finding_ref": "ATK-002"}]
        result = run_assign(json.dumps(items), "DEF")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output[0]["id"], "DEF-001")
        self.assertEqual(output[1]["id"], "DEF-002")

    def test_empty_array(self):
        result = run_assign("[]", "ATK")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

    def test_empty_stdin(self):
        result = run_assign("", "ATK")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

    def test_invalid_json_fails(self):
        result = run_assign("not json", "ATK")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid JSON", result.stderr)

    def test_non_array_fails(self):
        result = run_assign('{"key": "value"}', "ATK")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Expected a JSON array", result.stderr)

    def test_preserves_existing_fields(self):
        items = [{"category": "security", "description": "XSS vuln", "severity": "high"}]
        result = run_assign(json.dumps(items), "ATK")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output[0]["category"], "security")
        self.assertEqual(output[0]["description"], "XSS vuln")
        self.assertEqual(output[0]["severity"], "high")
        self.assertEqual(output[0]["id"], "ATK-001")

    def test_three_digit_padding(self):
        items = [{"x": i} for i in range(12)]
        result = run_assign(json.dumps(items), "ATK")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output[0]["id"], "ATK-001")
        self.assertEqual(output[9]["id"], "ATK-010")
        self.assertEqual(output[11]["id"], "ATK-012")

    def test_lens_prefix_assigns_lens_tagged_ids(self):
        items = [{"lens": "security"}, {"lens": "security"}]
        result = run_assign(json.dumps(items), "ATK-SEC")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output[0]["id"], "ATK-SEC-001")
        self.assertEqual(output[1]["id"], "ATK-SEC-002")

    def test_con_prefix_assigns_sequential_ids(self):
        items = [{"description": "merged finding"}, {"description": "another"}]
        result = run_assign(json.dumps(items), "CON")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output[0]["id"], "CON-001")
        self.assertEqual(output[1]["id"], "CON-002")

    def test_invalid_prefix_rejected(self):
        result = run_assign('[{"x": 1}]', "BOGUS")
        self.assertEqual(result.returncode, 2)  # argparse type error
        self.assertIn("invalid prefix", result.stderr)

    def test_lowercase_lens_prefix_rejected(self):
        result = run_assign('[{"x": 1}]', "ATK-sec")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid prefix", result.stderr)


if __name__ == "__main__":
    unittest.main()
