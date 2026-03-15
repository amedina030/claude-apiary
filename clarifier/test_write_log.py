#!/usr/bin/env python3
"""Tests for clarifier/write_log.py — all three modes: init, append, complete."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "write_log.py"
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(payload: dict, mode: str = "init", tmp_dir: Path = None) -> tuple[int, dict, str]:
    """Run write_log.py and return (returncode, parsed_output, stderr)."""
    payload_file = tmp_dir / f"payload_{mode}.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    cmd = [PYTHON, str(SCRIPT)]
    if mode == "append":
        cmd.append("--append")
    elif mode == "complete":
        cmd.append("--complete")
    cmd.append(str(payload_file))

    result = subprocess.run(cmd, text=True, capture_output=True)
    parsed = {}
    for line in result.stdout.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            parsed[k.strip()] = v.strip()
    return result.returncode, parsed, result.stderr


def init_payload(log_dir: Path, **kwargs) -> dict:
    base = {
        "log_dir": str(log_dir),
        "cwd": "",
        "original_prompt": "clean up the config file",
        "interpretation": "improve a config file",
        "detected_ambiguities": ["Which file?", "What does clean up mean?"],
        "intended_plan": "cannot proceed without clarification",
        "first_questions": "1. Which file?\n2. What does clean up mean?",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Init tests
# ---------------------------------------------------------------------------

def test_init_generates_uuid_and_filename(tmp_path):
    rc, out, err = run(init_payload(tmp_path / "logs"), tmp_dir=tmp_path)
    assert rc == 0, f"init failed: {err}"
    assert "uuid" in out and len(out["uuid"]) == 36
    assert "log" in out and out["log"].startswith("clarifier-")
    assert out["round_count"] == "0"
    assert out["status"] == "in_progress"


def test_init_writes_md_and_json_and_current(tmp_path):
    log_dir = tmp_path / "logs"
    rc, out, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    assert rc == 0
    assert (log_dir / out["log"]).exists(), "markdown not written"
    state_file = log_dir / (Path(out["log"]).stem + ".json")
    assert state_file.exists(), "state JSON not written"
    assert (log_dir / ".current").exists(), ".current pointer not written"


def test_init_creates_log_dir_if_missing(tmp_path):
    log_dir = tmp_path / "deep" / "nested" / "logs"
    assert not log_dir.exists()
    rc, _, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    assert rc == 0
    assert log_dir.exists()


def test_init_md_contains_required_sections(tmp_path):
    log_dir = tmp_path / "logs"
    rc, out, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    assert rc == 0
    content = (log_dir / out["log"]).read_text(encoding="utf-8")
    for section in [
        "## Original Prompt", "## Executing Agent Input",
        "## Clarification Rounds", "## Final Approved Prompt", "## Outcome",
    ]:
        assert section in content, f"Missing: {section}"
    assert "clean up the config file" in content
    assert "In Progress" in content
    assert "1. Which file?" in content


def test_init_auto_detects_log_dir_from_cwd(tmp_path):
    """If cwd has a .claude dir, log goes there instead of ~/.claude."""
    fake_cwd = tmp_path / "project"
    (fake_cwd / ".claude").mkdir(parents=True)
    rc, out, _ = run(
        {"cwd": str(fake_cwd), "original_prompt": "test",
         "interpretation": "t", "detected_ambiguities": [], "intended_plan": "t",
         "first_questions": "Q?"},
        tmp_dir=tmp_path,
    )
    assert rc == 0
    expected_log_dir = fake_cwd / ".claude" / "clarifier-logs"
    assert expected_log_dir.exists()
    assert (expected_log_dir / out["log"]).exists()


# ---------------------------------------------------------------------------
# Append tests
# ---------------------------------------------------------------------------

def test_append_completes_last_round_and_adds_new(tmp_path):
    log_dir = tmp_path / "logs"
    rc, out, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    assert rc == 0

    append_data = {
        "log_dir": str(log_dir),
        "responses": "The settings.json file. Clean means remove unused keys.",
        "updated_prompt": "Remove unused keys from settings.json",
        "new_questions": "3. Which keys are unused?",
    }
    rc2, out2, err2 = run(append_data, mode="append", tmp_dir=tmp_path)
    assert rc2 == 0, f"append failed: {err2}"
    assert out2["round_count"] == "1"   # round 1 is now complete
    assert out2["status"] == "in_progress"

    state_file = log_dir / (Path(out["log"]).stem + ".json")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(state["rounds"]) == 2
    assert state["rounds"][0]["responses"] == "The settings.json file. Clean means remove unused keys."
    assert state["rounds"][1]["questions"] == "3. Which keys are unused?"
    assert state["rounds"][1]["responses"] == "[pending]"


def test_append_increments_round_count(tmp_path):
    log_dir = tmp_path / "logs"
    run(init_payload(log_dir), tmp_dir=tmp_path)

    for i in range(3):
        data = {
            "log_dir": str(log_dir),
            "responses": f"Answer {i+1}",
            "updated_prompt": f"Prompt v{i+2}",
            "new_questions": f"Question {i+2}?",
        }
        rc, out, _ = run(data, mode="append", tmp_dir=tmp_path)
        assert rc == 0
        assert out["round_count"] == str(i + 1)


def test_append_updates_markdown(tmp_path):
    log_dir = tmp_path / "logs"
    _, out0, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    run(
        {"log_dir": str(log_dir), "responses": "settings.json",
         "updated_prompt": "fix settings.json", "new_questions": "What to fix?"},
        mode="append", tmp_dir=tmp_path,
    )
    content = (log_dir / out0["log"]).read_text(encoding="utf-8")
    assert "settings.json" in content
    assert "What to fix?" in content


# ---------------------------------------------------------------------------
# Complete tests
# ---------------------------------------------------------------------------

def test_complete_finalizes_session(tmp_path):
    log_dir = tmp_path / "logs"
    rc, out, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    assert rc == 0

    complete_data = {
        "log_dir": str(log_dir),
        "responses": "The settings.json file, remove unused keys only.",
        "final_prompt": "Remove unused keys from settings.json",
        "outcome": "Fully resolved",
        "unresolved_ambiguities": "None",
    }
    rc2, out2, err2 = run(complete_data, mode="complete", tmp_dir=tmp_path)
    assert rc2 == 0, f"complete failed: {err2}"
    assert out2["status"] == "complete"
    assert out2["round_count"] == "1"


def test_complete_removes_current_pointer(tmp_path):
    log_dir = tmp_path / "logs"
    run(init_payload(log_dir), tmp_dir=tmp_path)
    assert (log_dir / ".current").exists()

    run(
        {"log_dir": str(log_dir), "final_prompt": "done", "outcome": "Fully resolved"},
        mode="complete", tmp_dir=tmp_path,
    )
    assert not (log_dir / ".current").exists(), ".current should be deleted after complete"


def test_complete_writes_complete_status_to_md(tmp_path):
    log_dir = tmp_path / "logs"
    _, out, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    run(
        {"log_dir": str(log_dir), "final_prompt": "Remove unused keys from settings.json",
         "outcome": "Fully resolved", "unresolved_ambiguities": "None"},
        mode="complete", tmp_dir=tmp_path,
    )
    content = (log_dir / out["log"]).read_text(encoding="utf-8")
    assert "Complete" in content
    assert "In Progress" not in content
    assert "Remove unused keys from settings.json" in content


def test_complete_preserves_uuid_from_init(tmp_path):
    log_dir = tmp_path / "logs"
    _, out_init, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    _, out_complete, _ = run(
        {"log_dir": str(log_dir), "final_prompt": "done", "outcome": "Fully resolved"},
        mode="complete", tmp_dir=tmp_path,
    )
    assert out_init["uuid"] == out_complete["uuid"]
    assert out_init["log"] == out_complete["log"]


# ---------------------------------------------------------------------------
# Round count tests
# ---------------------------------------------------------------------------

def test_round_count_is_zero_at_init(tmp_path):
    log_dir = tmp_path / "logs"
    _, out, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    assert out["round_count"] == "0"


def test_round_count_only_counts_completed_rounds(tmp_path):
    """round_count reflects completed (responded) rounds, not total rounds."""
    log_dir = tmp_path / "logs"
    _, out_init, _ = run(init_payload(log_dir), tmp_dir=tmp_path)
    # One round open, no responses yet → round_count = 0
    assert out_init["round_count"] == "0"

    # Append (completes round 1, opens round 2) → round_count = 1
    _, out_append, _ = run(
        {"log_dir": str(log_dir), "responses": "answer", "updated_prompt": "p2", "new_questions": "q2"},
        mode="append", tmp_dir=tmp_path,
    )
    assert out_append["round_count"] == "1"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_init_generates_uuid_and_filename,
        test_init_writes_md_and_json_and_current,
        test_init_creates_log_dir_if_missing,
        test_init_md_contains_required_sections,
        test_init_auto_detects_log_dir_from_cwd,
        test_append_completes_last_round_and_adds_new,
        test_append_increments_round_count,
        test_append_updates_markdown,
        test_complete_finalizes_session,
        test_complete_removes_current_pointer,
        test_complete_writes_complete_status_to_md,
        test_complete_preserves_uuid_from_init,
        test_round_count_is_zero_at_init,
        test_round_count_only_counts_completed_rounds,
    ]

    import tempfile
    passed = 0
    with tempfile.TemporaryDirectory() as td:
        for test in tests:
            tmp = Path(td) / test.__name__
            tmp.mkdir()
            name = test.__name__.replace("test_", "").replace("_", " ")
            print(f"  {name:<50} ", end="")
            try:
                test(tmp)
                print("OK")
                passed += 1
            except Exception as e:
                print(f"FAIL — {e}")

    print(f"\n{passed}/{len(tests)} passed.")
    if passed < len(tests):
        sys.exit(1)


if __name__ == "__main__":
    main()
