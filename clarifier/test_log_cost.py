#!/usr/bin/env python3
"""Tests for clarifier/log_cost.py — tally and finalize subcommands."""

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SCRIPT = Path(__file__).parent / "log_cost.py"
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_tally(session_id: str, tokens: int, tools: int, duration: int,
              logs_dir: Path) -> tuple[int, str]:
    result = subprocess.run(
        [PYTHON, str(SCRIPT), "tally",
         "--id", session_id,
         "--tokens", str(tokens),
         "--tools", str(tools),
         "--duration", str(duration),
         "--logs-dir", str(logs_dir)],
        text=True, capture_output=True,
    )
    return result.returncode, result.stdout + result.stderr


def run_finalize(session_id: str, log_file: str, prompt: str,
                 logs_dir: Path, explicit_session_id: str = None,
                 budgeter_tmp: Path = None) -> tuple[int, str]:
    cmd = [PYTHON, str(SCRIPT), "finalize",
           "--id", session_id,
           "--log", log_file,
           "--prompt", prompt,
           "--logs-dir", str(logs_dir)]
    if explicit_session_id:
        cmd += ["--session-id", explicit_session_id]
    if budgeter_tmp:
        cmd += ["--budgeter-tmp", str(budgeter_tmp)]
    result = subprocess.run(cmd, text=True, capture_output=True)
    return result.returncode, result.stdout + result.stderr


def make_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tally tests
# ---------------------------------------------------------------------------

def test_tally_creates_tally_file(tmp_path):
    sid = make_id()
    rc, out = run_tally(sid, tokens=1000, tools=3, duration=500, logs_dir=tmp_path)
    assert rc == 0, f"tally failed: {out}"
    assert (tmp_path / f".tally-{sid}.json").exists(), "Tally file not created"


def test_tally_accumulates_across_calls(tmp_path):
    sid = make_id()
    run_tally(sid, tokens=1000, tools=2, duration=300, logs_dir=tmp_path)
    run_tally(sid, tokens=500,  tools=1, duration=200, logs_dir=tmp_path)
    run_tally(sid, tokens=750,  tools=3, duration=400, logs_dir=tmp_path)

    tally = json.loads((tmp_path / f".tally-{sid}.json").read_text())
    assert tally["total_tokens"] == 2250
    assert tally["total_tools"] == 6
    assert tally["total_duration_ms"] == 900
    assert tally["calls"] == 3


def test_tally_does_not_mix_sessions(tmp_path):
    sid1, sid2 = make_id(), make_id()
    run_tally(sid1, tokens=1000, tools=2, duration=300, logs_dir=tmp_path)
    run_tally(sid2, tokens=500,  tools=1, duration=100, logs_dir=tmp_path)

    t1 = json.loads((tmp_path / f".tally-{sid1}.json").read_text())
    t2 = json.loads((tmp_path / f".tally-{sid2}.json").read_text())
    assert t1["total_tokens"] == 1000
    assert t2["total_tokens"] == 500


# ---------------------------------------------------------------------------
# Finalize tests
# ---------------------------------------------------------------------------

def test_finalize_writes_cost_log(tmp_path):
    sid = make_id()
    run_tally(sid, tokens=1000, tools=2, duration=500, logs_dir=tmp_path)
    rc, out = run_finalize(sid, "clarifier-2026-03-14-214013-a3f2.md",
                           "clean up the config file", tmp_path)
    assert rc == 0, f"finalize failed: {out}"
    cost_log = tmp_path / "cost.log"
    assert cost_log.exists(), "cost.log not created"
    entry = cost_log.read_text()
    assert sid in entry
    assert "total_tokens: 1000" in entry
    assert "tool_uses: 2" in entry
    assert "duration_ms: 500" in entry
    assert "clarifier-2026-03-14-214013-a3f2.md" in entry
    assert "clean up the config file" in entry


def test_finalize_removes_tally_file(tmp_path):
    sid = make_id()
    run_tally(sid, tokens=500, tools=1, duration=200, logs_dir=tmp_path)
    tally_file = tmp_path / f".tally-{sid}.json"
    assert tally_file.exists()

    run_finalize(sid, "clarifier-test.md", "test prompt", tmp_path)
    assert not tally_file.exists(), "Tally file should be deleted after finalize"


def test_finalize_sums_multiple_tally_calls(tmp_path):
    sid = make_id()
    run_tally(sid, tokens=1000, tools=2, duration=300, logs_dir=tmp_path)
    run_tally(sid, tokens=2000, tools=4, duration=700, logs_dir=tmp_path)
    run_finalize(sid, "clarifier-test.md", "test", tmp_path)

    entry = (tmp_path / "cost.log").read_text()
    assert "total_tokens: 3000" in entry
    assert "tool_uses: 6" in entry
    assert "duration_ms: 1000" in entry


def test_finalize_truncates_prompt_to_80_chars(tmp_path):
    sid = make_id()
    long_prompt = "x" * 120
    run_finalize(sid, "clarifier-test.md", long_prompt, tmp_path)

    entry = (tmp_path / "cost.log").read_text()
    start = entry.index('prompt: "') + len('prompt: "')
    end = entry.index('"', start)
    snippet = entry[start:end]
    assert len(snippet) <= 80, f"Prompt not truncated: {len(snippet)} chars"


def test_finalize_appends_to_existing_cost_log(tmp_path):
    sid1, sid2 = make_id(), make_id()
    run_finalize(sid1, "log1.md", "prompt one", tmp_path)
    run_finalize(sid2, "log2.md", "prompt two", tmp_path)

    lines = (tmp_path / "cost.log").read_text().strip().splitlines()
    assert len(lines) == 2, f"Expected 2 entries, got {len(lines)}"


def test_finalize_works_without_prior_tally(tmp_path):
    """finalize should not crash if tally was never called (defaults to zeros)."""
    sid = make_id()
    rc, out = run_finalize(sid, "clarifier-test.md", "test", tmp_path)
    assert rc == 0, f"finalize without tally failed: {out}"
    entry = (tmp_path / "cost.log").read_text()
    assert "total_tokens: 0" in entry


def test_finalize_writes_explicit_session_id(tmp_path):
    sid = make_id()
    claude_sid = str(uuid.uuid4())
    run_finalize(sid, "clarifier-test.md", "test", tmp_path, explicit_session_id=claude_sid)
    entry = (tmp_path / "cost.log").read_text()
    assert f"session_id: {claude_sid}" in entry


def test_finalize_autodetects_session_id_from_budgeter_tmp(tmp_path):
    sid = make_id()
    claude_sid = str(uuid.uuid4())
    budgeter_tmp = tmp_path / "budgeter_tmp"
    budgeter_tmp.mkdir()
    (budgeter_tmp / f"{claude_sid}_baseline.json").write_text("{}")
    run_finalize(sid, "clarifier-test.md", "test", tmp_path, budgeter_tmp=budgeter_tmp)
    entry = (tmp_path / "cost.log").read_text()
    assert f"session_id: {claude_sid}" in entry


def test_finalize_writes_unknown_when_no_session_id(tmp_path):
    sid = make_id()
    empty_tmp = tmp_path / "empty"
    empty_tmp.mkdir()
    run_finalize(sid, "clarifier-test.md", "test", tmp_path, budgeter_tmp=empty_tmp)
    entry = (tmp_path / "cost.log").read_text()
    assert "session_id: unknown" in entry


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_tally_creates_tally_file,
        test_tally_accumulates_across_calls,
        test_tally_does_not_mix_sessions,
        test_finalize_writes_cost_log,
        test_finalize_removes_tally_file,
        test_finalize_sums_multiple_tally_calls,
        test_finalize_truncates_prompt_to_80_chars,
        test_finalize_appends_to_existing_cost_log,
        test_finalize_works_without_prior_tally,
        test_finalize_writes_explicit_session_id,
        test_finalize_autodetects_session_id_from_budgeter_tmp,
        test_finalize_writes_unknown_when_no_session_id,
    ]

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
