#!/usr/bin/env python3
"""Integration tests for harden/validate_and_assign.py — the combined
validate + assign-IDs entry point used by the /harden orchestrator."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "validate_and_assign.py")
PYTHON = sys.executable


def run(args: list, input_json: str) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, SCRIPT] + args, input=input_json, text=True, capture_output=True)


class TestFindingsLensMode(unittest.TestCase):
    def test_lens_findings_get_lens_tagged_ids(self):
        findings = [
            {"severity": "high", "description": "SQLi in query", "location": "db.py:10"},
            {"severity": "low", "description": "weak rng", "location": "auth.py:5"},
        ]
        result = run(["findings", "--lens", "security", "--sanitize"], json.dumps(findings))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out[0]["id"], "ATK-SEC-001")
        self.assertEqual(out[1]["id"], "ATK-SEC-002")
        self.assertEqual(out[0]["lens"], "security")

    def test_legacy_findings_still_get_plain_atk_ids(self):
        findings = [
            {
                "category": "security",
                "severity": "high",
                "description": "SQLi",
                "location": "db.py:10",
            }
        ]
        result = run(["findings", "--sanitize"], json.dumps(findings))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out[0]["id"], "ATK-001")

    def test_invalid_lens_rejected(self):
        result = run(["findings", "--lens", "bogus", "--sanitize"], "[]")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid lens", result.stderr)


class TestConsolidationCommand(unittest.TestCase):
    def test_accepted_get_con_ids(self):
        data = {
            "accepted": [
                {
                    "description": "a",
                    "severity": "high",
                    "location": "x.py:1",
                    "source_ids": ["ATK-SEC-001"],
                },
                {
                    "description": "b",
                    "severity": "low",
                    "location": "y.py:2",
                    "source_ids": ["ATK-COR-001"],
                },
            ],
            "rejected": [],
        }
        result = run(["consolidation", "--source-ids", "ATK-SEC-001,ATK-COR-001"], json.dumps(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["accepted"][0]["id"], "CON-001")
        self.assertEqual(out["accepted"][1]["id"], "CON-002")

    def test_coverage_gap_fails(self):
        data = {
            "accepted": [
                {
                    "description": "a",
                    "severity": "high",
                    "location": "x.py:1",
                    "source_ids": ["ATK-SEC-001"],
                }
            ],
            "rejected": [],
        }
        result = run(["consolidation", "--source-ids", "ATK-SEC-001,ATK-COR-002"], json.dumps(data))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ATK-COR-002 not accounted for", result.stderr)

    def test_degrade_assigns_con_ids(self):
        merged = [
            {
                "id": "ATK-SEC-001",
                "lens": "security",
                "severity": "high",
                "description": "x",
                "location": "a.py:1",
            },
            {
                "id": "ATK-COR-001",
                "lens": "correctness",
                "severity": "low",
                "description": "y",
                "location": "a.py:1",
            },
        ]
        result = run(["consolidation", "--degrade"], json.dumps(merged))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(len(out["accepted"]), 1)
        self.assertEqual(out["accepted"][0]["id"], "CON-001")

    def test_defender_response_against_con_ids(self):
        # The defender addresses CON-NNN ids; validate_and_assign response is prefix-agnostic.
        data = {
            "responses": [
                {
                    "finding_ref": "CON-001",
                    "action": "fixed",
                    "description": "patched",
                    "changes": [],
                },
            ],
            "todos": [],
        }
        result = run(["response", "--expected-ids", "CON-001"], json.dumps(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["responses"][0]["id"], "DEF-001")


if __name__ == "__main__":
    unittest.main()
