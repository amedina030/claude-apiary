#!/usr/bin/env python3
"""Tests for harden/validate_consolidation.py."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).parent / "validate_consolidation.py")
PYTHON = sys.executable


def run(
    input_json: str, source_ids: str = None, check_files: bool = False, degrade: bool = False
) -> subprocess.CompletedProcess:
    cmd = [PYTHON, SCRIPT]
    if source_ids:
        cmd += ["--source-ids", source_ids]
    if check_files:
        cmd.append("--check-files")
    if degrade:
        cmd.append("--degrade")
    return subprocess.run(cmd, input=input_json, text=True, capture_output=True)


def make_accepted(**overrides):
    base = {
        "description": "Unsanitized path joins user input",
        "severity": "high",
        "location": "app/files.py:12-18",
        "source_ids": ["ATK-SEC-001"],
        "lenses": ["security"],
    }
    base.update(overrides)
    return base


def make_consolidation(**overrides):
    base = {"accepted": [make_accepted()], "rejected": []}
    base.update(overrides)
    return base


class TestValidateConsolidation(unittest.TestCase):
    def test_valid_passes(self):
        result = run(json.dumps(make_consolidation()))
        self.assertEqual(result.returncode, 0)

    def test_accepted_with_id_fails(self):
        c = make_consolidation()
        c["accepted"][0]["id"] = "CON-001"
        result = run(json.dumps(c))
        self.assertEqual(result.returncode, 1)
        self.assertIn("must not include an 'id'", result.stderr)

    def test_missing_source_ids_fails(self):
        acc = make_accepted()
        del acc["source_ids"]
        result = run(json.dumps({"accepted": [acc], "rejected": []}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing required field 'source_ids'", result.stderr)

    def test_empty_source_ids_fails(self):
        result = run(json.dumps({"accepted": [make_accepted(source_ids=[])], "rejected": []}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("must not be empty", result.stderr)

    def test_invalid_severity_fails(self):
        result = run(json.dumps({"accepted": [make_accepted(severity="urgent")], "rejected": []}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid severity", result.stderr)

    def test_rejected_requires_reason(self):
        c = {"accepted": [], "rejected": [{"source_ids": ["ATK-COR-001"]}]}
        result = run(json.dumps(c))
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing required field 'reason'", result.stderr)

    def test_rejected_with_reason_passes(self):
        c = {
            "accepted": [],
            "rejected": [
                {"source_ids": ["ATK-COR-001"], "reason": "not substantiated by cited code"}
            ],
        }
        result = run(json.dumps(c))
        self.assertEqual(result.returncode, 0)

    def test_coverage_missing_source_fails(self):
        # dispatched two, only one accounted for
        result = run(json.dumps(make_consolidation()), source_ids="ATK-SEC-001,ATK-COR-002")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ATK-COR-002 not accounted for", result.stderr)

    def test_coverage_unknown_source_fails(self):
        result = run(json.dumps(make_consolidation()), source_ids="ATK-COR-002")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown source id ATK-SEC-001", result.stderr)

    def test_coverage_exact_passes(self):
        c = {
            "accepted": [make_accepted(source_ids=["ATK-SEC-001", "ATK-ROB-001"])],
            "rejected": [{"source_ids": ["ATK-COR-002"], "reason": "false positive"}],
        }
        result = run(json.dumps(c), source_ids="ATK-SEC-001,ATK-ROB-001,ATK-COR-002")
        self.assertEqual(result.returncode, 0)

    def test_duplicate_source_id_fails(self):
        c = {
            "accepted": [make_accepted(source_ids=["ATK-SEC-001"])],
            "rejected": [{"source_ids": ["ATK-SEC-001"], "reason": "dup"}],
        }
        result = run(json.dumps(c))
        self.assertEqual(result.returncode, 1)
        self.assertIn("referenced more than once", result.stderr)

    def test_check_files_missing_file_fails(self):
        result = run(
            json.dumps(
                {"accepted": [make_accepted(location="nope/missing.py:1-2")], "rejected": []}
            ),
            check_files=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("file not found", result.stderr)

    def test_all_rejected_empty_accepted_passes(self):
        c = {"accepted": [], "rejected": [{"source_ids": ["ATK-COR-001"], "reason": "benign"}]}
        result = run(json.dumps(c))
        self.assertEqual(result.returncode, 0)

    def test_invalid_json_fails(self):
        result = run("not json")
        self.assertEqual(result.returncode, 1)

    def test_degrade_dedups_by_location(self):
        merged = [
            {
                "id": "ATK-SEC-001",
                "lens": "security",
                "severity": "medium",
                "description": "path issue",
                "location": "app/files.py:12",
            },
            {
                "id": "ATK-COR-001",
                "lens": "correctness",
                "severity": "high",
                "description": "off by one",
                "location": "app/files.py:12",
            },
            {
                "id": "ATK-RES-001",
                "lens": "resilience",
                "severity": "low",
                "description": "leak",
                "location": "app/net.py:5",
            },
        ]
        result = run(json.dumps(merged), degrade=True)
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)
        self.assertEqual(out["rejected"], [])
        accepted = out["accepted"]
        self.assertEqual(len(accepted), 2)  # two distinct locations
        merged_entry = next(a for a in accepted if a["location"] == "app/files.py:12")
        # highest severity wins, both source ids + lenses collected
        self.assertEqual(merged_entry["severity"], "high")
        self.assertEqual(set(merged_entry["source_ids"]), {"ATK-SEC-001", "ATK-COR-001"})
        self.assertEqual(set(merged_entry["lenses"]), {"security", "correctness"})


if __name__ == "__main__":
    unittest.main()
