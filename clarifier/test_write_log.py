#!/usr/bin/env python3
"""Tests for clarifier/write_log.py."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "write_log.py"
PYTHON = sys.executable


def run(payload: dict) -> tuple[int, str, str]:
    result = subprocess.run(
        [PYTHON, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    return result.returncode, result.stdout, result.stderr


def parse_output(stdout: str) -> dict:
    out = {}
    for line in stdout.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_generates_uuid_and_filename(tmp_path):
    """Script generates uuid and filename when not provided."""
    rc, out, err = run({"log_dir": str(tmp_path), "status": "in_progress",
                        "original_prompt": "test"})
    assert rc == 0, f"Script failed: {err}"
    parsed = parse_output(out)
    assert "uuid" in parsed, "Output must include uuid"
    assert "log" in parsed, "Output must include log filename"
    assert parsed["log"].startswith("clarifier-"), f"Unexpected filename: {parsed['log']}"
    assert len(parsed["uuid"]) == 36, f"UUID wrong length: {parsed['uuid']}"


def test_respects_provided_uuid_and_filename(tmp_path):
    """Script uses provided uuid and filename without generating new ones."""
    rc, out, _ = run({
        "log_dir": str(tmp_path),
        "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "filename": "clarifier-test-fixed.md",
        "status": "in_progress",
        "original_prompt": "test",
    })
    assert rc == 0
    parsed = parse_output(out)
    assert parsed["uuid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert parsed["log"] == "clarifier-test-fixed.md"


def test_writes_file_to_log_dir(tmp_path):
    """Log file is written to the specified directory."""
    rc, out, _ = run({"log_dir": str(tmp_path), "status": "in_progress",
                      "original_prompt": "test"})
    assert rc == 0
    parsed = parse_output(out)
    log_file = tmp_path / parsed["log"]
    assert log_file.exists(), f"Log file not found: {log_file}"


def test_creates_log_dir_if_missing(tmp_path):
    """Script creates the log directory if it does not exist."""
    new_dir = tmp_path / "subdir" / "logs"
    assert not new_dir.exists()
    rc, out, _ = run({"log_dir": str(new_dir), "status": "in_progress",
                      "original_prompt": "test"})
    assert rc == 0
    assert new_dir.exists()


def test_log_contains_required_fields(tmp_path):
    """Log file contains all required sections."""
    rc, out, _ = run({
        "log_dir": str(tmp_path),
        "status": "in_progress",
        "original_prompt": "clean up the config file",
        "interpretation": "improve a config file",
        "detected_ambiguities": ["Which file?", "What does clean up mean?"],
        "intended_plan": "cannot proceed without clarification",
        "rounds": [{"questions": "Q1\nQ2", "responses": "[pending]", "updated_prompt": "[pending]"}],
    })
    assert rc == 0
    parsed = parse_output(out)
    content = (tmp_path / parsed["log"]).read_text(encoding="utf-8")
    for section in ["## Original Prompt", "## Executing Agent Input",
                    "## Clarification Rounds", "## Final Approved Prompt", "## Outcome"]:
        assert section in content, f"Missing section: {section}"
    assert "clean up the config file" in content
    assert "In Progress" in content


def test_overwrite_preserves_uuid(tmp_path):
    """Calling the script twice with the same uuid/filename overwrites the file in place."""
    fixed_uuid = "12345678-1234-1234-1234-123456789abc"
    fixed_file = "clarifier-overwrite-test.md"

    rc, _, _ = run({
        "log_dir": str(tmp_path), "uuid": fixed_uuid, "filename": fixed_file,
        "status": "in_progress", "original_prompt": "first write",
    })
    assert rc == 0
    assert (tmp_path / fixed_file).exists()

    rc, out, _ = run({
        "log_dir": str(tmp_path), "uuid": fixed_uuid, "filename": fixed_file,
        "status": "complete", "original_prompt": "first write",
        "final_prompt": "final resolved prompt", "outcome": "Fully resolved",
    })
    assert rc == 0
    content = (tmp_path / fixed_file).read_text(encoding="utf-8")
    assert "Complete" in content
    assert "final resolved prompt" in content
    assert fixed_uuid in content


def test_complete_status_in_log(tmp_path):
    """Status 'complete' renders as 'Complete' in the log file."""
    rc, out, _ = run({
        "log_dir": str(tmp_path), "status": "complete",
        "original_prompt": "test", "final_prompt": "do X in file Y",
        "outcome": "Fully resolved",
    })
    assert rc == 0
    parsed = parse_output(out)
    content = (tmp_path / parsed["log"]).read_text(encoding="utf-8")
    assert "Complete" in content
    assert "In Progress" not in content


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_generates_uuid_and_filename,
        test_respects_provided_uuid_and_filename,
        test_writes_file_to_log_dir,
        test_creates_log_dir_if_missing,
        test_log_contains_required_fields,
        test_overwrite_preserves_uuid,
        test_complete_status_in_log,
    ]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        passed = 0
        for test in tests:
            name = test.__name__.replace("test_", "").replace("_", " ")
            print(f"  {name:<45} ", end="")
            try:
                test(tmp / test.__name__)
                print("OK")
                passed += 1
            except Exception as e:
                print(f"FAIL — {e}")

    print(f"\n{passed}/{len(tests)} passed.")
    if passed < len(tests):
        sys.exit(1)


if __name__ == "__main__":
    main()
