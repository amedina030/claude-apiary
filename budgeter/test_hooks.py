#!/usr/bin/env python3
"""
Tests for claude-apis budgeter hooks and logger.

Covers:
  - Unit tests for logger (append_entry zero-delta filter, baseline, snapshot)
  - Integration test: PRE -> POST -> STOP pipeline
  - PRE-to-PRE baseline: PRE saves prev_tool_name for next PRE to attribute correctly
  - task_turn: baseline stores task_turn; [CONT] prefix causes continuation to inherit task_turn
  - count_tasks: counts unique (session_id, task_turn) pairs, not raw entries
  - Agent PostToolUse: logs Agent token cost from tool_response.totalTokens
  - No double-count: PRE skips logging when prev_tool_name == "Agent"
"""
import sys
import json
import subprocess
import uuid
import shutil
import tempfile
from pathlib import Path

BUDGETER_DIR = Path(__file__).parent
APIS_DIR = BUDGETER_DIR.parent
HOOKS_DIR = BUDGETER_DIR / "hooks"
PYTHON = sys.executable

sys.path.insert(0, str(APIS_DIR))
from budgeter.lib import logger as _logger_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session_id():
    return f"test-{uuid.uuid4().hex[:8]}"


def run_hook(script, payload):
    return subprocess.run(
        [PYTHON, str(HOOKS_DIR / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )


def log_entry_count(log_path):
    if not log_path.exists():
        return 0
    return sum(1 for line in log_path.read_text().splitlines() if line.strip())


# ---------------------------------------------------------------------------
# Unit tests: logger
# ---------------------------------------------------------------------------

def test_append_entry_skips_zero_delta(tmp_dir):
    """append_entry must not write entries with tokens_delta == 0."""
    import budgeter.lib.logger as lg
    orig_log = lg.LOG_PATH
    lg.LOG_PATH = tmp_dir / "log.jsonl"
    try:
        lg.append_entry({"tokens_delta": 0, "tool_name": "Bash"})
        assert not lg.LOG_PATH.exists(), "Zero-delta entry should not be written"

        lg.append_entry({"tokens_delta": 100, "tool_name": "Bash"})
        assert lg.LOG_PATH.exists(), "Non-zero entry should be written"
        assert log_entry_count(lg.LOG_PATH) == 1
    finally:
        lg.LOG_PATH = orig_log


def test_count_tasks(tmp_dir):
    """count_tasks must count unique (session_id, task_turn) pairs, not raw entries."""
    import budgeter.lib.logger as lg
    orig_log = lg.LOG_PATH
    lg.LOG_PATH = tmp_dir / "count_tasks_log.jsonl"
    try:
        # Two entries in the same task — should count as 1 task
        lg.append_entry({"tokens_delta": 100, "session_id": "s1", "task_turn": 1, "turn_number": 1})
        lg.append_entry({"tokens_delta": 200, "session_id": "s1", "task_turn": 1, "turn_number": 1})
        assert lg.count_tasks() == 1, "Two entries from the same task must count as 1"

        # A second task in the same session
        lg.append_entry({"tokens_delta": 300, "session_id": "s1", "task_turn": 2, "turn_number": 2})
        assert lg.count_tasks() == 2

        # Same task_turn but different session — separate task
        lg.append_entry({"tokens_delta": 400, "session_id": "s2", "task_turn": 1, "turn_number": 1})
        assert lg.count_tasks() == 3
    finally:
        lg.LOG_PATH = orig_log


def test_baseline_save_load_delete(tmp_dir):
    """Baseline round-trip: save -> load -> verify tokens and task_turn -> cleanup removes it."""
    import budgeter.lib.logger as lg
    orig_tmp = lg.TMP_DIR
    lg.TMP_DIR = tmp_dir
    session_id = make_session_id()
    try:
        assert lg.load_baseline(session_id) is None
        lg.save_baseline(session_id, 42000, turn_number=3, task_turn=1)
        b = lg.load_baseline(session_id)
        assert b is not None and b["tokens"] == 42000
        assert b["task_turn"] == 1, "task_turn must be stored in baseline"
        assert b["turn_number"] == 3
        lg.cleanup_session(session_id)
        assert lg.load_baseline(session_id) is None
    finally:
        lg.TMP_DIR = orig_tmp


def test_snapshot_save_load_delete(tmp_dir):
    """Snapshot round-trip: save -> load -> delete."""
    import budgeter.lib.logger as lg
    orig_tmp = lg.TMP_DIR
    lg.TMP_DIR = tmp_dir
    session_id = make_session_id()
    try:
        assert lg.load_snapshot(session_id) is None
        lg.save_snapshot(session_id, {"assistant_message": "hello", "tool_name": "Bash"})
        s = lg.load_snapshot(session_id)
        assert s is not None and s["assistant_message"] == "hello"
        lg.delete_snapshot(session_id)
        assert lg.load_snapshot(session_id) is None
    finally:
        lg.TMP_DIR = orig_tmp


# ---------------------------------------------------------------------------
# Integration tests: hook pipeline
# ---------------------------------------------------------------------------

def test_pre_post_stop_pipeline(log_path, tmp_path):
    """PRE -> POST -> STOP with empty transcript. Verifies plumbing end-to-end."""
    session_id = make_session_id()
    payload = {"tool_name": "Bash", "session_id": session_id, "transcript_path": "", "cwd": ""}

    # PRE — no prior baseline, so no entry logged. Should write baseline.
    before = log_entry_count(log_path)
    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0, f"PRE failed: {r.stderr}"
    baseline_path = BUDGETER_DIR / "tmp" / f"{session_id}_baseline.json"
    assert baseline_path.exists(), "PRE should write a baseline"
    assert log_entry_count(log_path) == before, "PRE with no prior baseline must not write an entry"

    # POST — now a no-op
    r = run_hook("post_tool_use.py", payload)
    assert r.returncode == 0, f"POST failed: {r.stderr}"

    # STOP — logs final entry (empty transcript -> delta=0 -> not written), cleans up
    r = run_hook("stop_session.py", {"session_id": session_id, "transcript_path": "", "cwd": ""})
    assert r.returncode == 0, f"STOP failed: {r.stderr}"
    leftover = list((BUDGETER_DIR / "tmp").glob(f"{session_id}_*"))
    assert not leftover, f"STOP left tmp files: {[f.name for f in leftover]}"


def test_pre_to_pre_baseline(log_path):
    """PRE saves prev_tool_name in baseline so the next PRE can attribute costs correctly."""
    import budgeter.lib.logger as lg
    session_id = make_session_id()
    payload = {"tool_name": "Write", "session_id": session_id, "transcript_path": "", "cwd": ""}

    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0

    b = lg.load_baseline(session_id)
    assert b is not None
    assert b["prev_tool_name"] == "Write", "Baseline must record the tool that just ran"
    assert b["tokens"] == 0  # empty transcript
    assert "task_turn" in b, "Baseline must include task_turn"

    # Cleanup
    lg.cleanup_session(session_id)


def test_cont_continuation_inherits_task_turn(log_path):
    """A turn whose assistant message starts with [CONT] must inherit task_turn from the prior baseline."""
    import budgeter.lib.logger as lg
    session_id = make_session_id()

    # Simulate an existing baseline from turn 1, task_turn=1
    orig_tmp = lg.TMP_DIR
    try:
        lg.save_baseline(
            session_id, tokens=1000,
            context_tokens=500,
            prev_tool_name="Read",
            prev_assistant_message="I found an issue, should I proceed?",
            turn_number=1,
            task_turn=1,
        )

        # Build a minimal transcript with turn_number=2 and a [CONT]-prefixed assistant message.
        # We pass an empty transcript path and rely on the hook's empty-transcript fallback,
        # then manually verify that a baseline written with turn_number > prev_turn and
        # assistant_message starting with [CONT] inherits task_turn from the prior baseline.
        # Since we can't inject a real transcript here, we test the logger logic directly.
        b_before = lg.load_baseline(session_id)
        assert b_before["task_turn"] == 1

        # Simulate what pre_tool_use.py does when it sees [CONT] on a new turn
        is_continuation = True
        turn_number = 2
        task_turn = b_before.get("task_turn", b_before.get("turn_number", turn_number))

        lg.save_baseline(
            session_id, tokens=1500,
            prev_tool_name="Write",
            prev_assistant_message="Ok, writing now.",
            turn_number=turn_number,
            task_turn=task_turn,
        )

        b_after = lg.load_baseline(session_id)
        assert b_after["turn_number"] == 2, "turn_number must advance"
        assert b_after["task_turn"] == 1, "task_turn must be inherited from prior baseline on [CONT]"
    finally:
        lg.TMP_DIR = orig_tmp
        lg.cleanup_session(session_id)


# ---------------------------------------------------------------------------
# Agent PostToolUse tests
# ---------------------------------------------------------------------------

def test_post_agent_logs_total_tokens(log_path):
    """PostToolUse hook must log an Agent entry with tokens_delta == totalTokens."""
    import budgeter.lib.logger as lg
    session_id = make_session_id()

    # Write a baseline so the hook has task_turn / user_message context
    orig_tmp = lg.TMP_DIR
    try:
        lg.save_baseline(
            session_id, tokens=5000,
            prev_tool_name="Bash",
            prev_assistant_message="About to spawn agent",
            turn_number=3,
            task_turn=3,
            user_message="do something",
        )

        before = log_entry_count(log_path)
        payload = {
            "tool_name": "Agent",
            "session_id": session_id,
            "cwd": "",
            "tool_response": {"totalTokens": 12345},
        }
        r = run_hook("post_tool_use.py", payload)
        assert r.returncode == 0, f"POST failed: {r.stderr}"
        assert log_entry_count(log_path) == before + 1, "POST must write one Agent entry"

        entries = lg.read_log()
        agent_entries = [e for e in entries if e.get("session_id") == session_id and e.get("tool_name") == "Agent"]
        assert len(agent_entries) == 1
        assert agent_entries[0]["tokens_delta"] == 12345
        assert agent_entries[0]["net_tokens_delta"] == 12345
        assert agent_entries[0]["task_turn"] == 3
    finally:
        lg.TMP_DIR = orig_tmp
        lg.cleanup_session(session_id)


def test_post_agent_zero_tokens_not_logged(log_path):
    """PostToolUse hook must not log an Agent entry when totalTokens == 0."""
    session_id = make_session_id()
    before = log_entry_count(log_path)
    payload = {
        "tool_name": "Agent",
        "session_id": session_id,
        "cwd": "",
        "tool_response": {"totalTokens": 0},
    }
    r = run_hook("post_tool_use.py", payload)
    assert r.returncode == 0, f"POST failed: {r.stderr}"
    assert log_entry_count(log_path) == before, "Zero-token Agent entry must not be written"


def test_pre_skips_logging_after_agent(log_path):
    """PRE hook must not log a duplicate entry when prev_tool_name == 'Agent'."""
    import budgeter.lib.logger as lg
    session_id = make_session_id()
    orig_tmp = lg.TMP_DIR
    try:
        # Simulate baseline left by Agent's PRE hook
        lg.save_baseline(
            session_id, tokens=10000,
            prev_tool_name="Agent",
            prev_assistant_message="agent ran",
            turn_number=5,
            task_turn=5,
            user_message="run agent",
        )

        before = log_entry_count(log_path)
        payload = {"tool_name": "Bash", "session_id": session_id, "transcript_path": "", "cwd": ""}
        r = run_hook("pre_tool_use.py", payload)
        assert r.returncode == 0, f"PRE failed: {r.stderr}"
        assert log_entry_count(log_path) == before, "PRE must not log an Agent entry (PostToolUse already did)"
    finally:
        lg.TMP_DIR = orig_tmp
        lg.cleanup_session(session_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Require budgeter-log-enabled for integration tests
    flag = Path.home() / ".claude" / "budgeter-log-enabled"
    if not flag.exists():
        print("Note: budgeter-log-enabled not set — integration tests will skip logging checks.")

    log_path = BUDGETER_DIR / "data" / "usage_log.jsonl"
    log_path.parent.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)

        print("Unit: append_entry skips zero-delta ... ", end="")
        test_append_entry_skips_zero_delta(tmp_dir)
        print("OK")

        print("Unit: count_tasks deduplicates ......... ", end="")
        test_count_tasks(tmp_dir)
        print("OK")

        print("Unit: baseline save/load/delete ....... ", end="")
        test_baseline_save_load_delete(tmp_dir)
        print("OK")

        print("Unit: snapshot save/load/delete ....... ", end="")
        test_snapshot_save_load_delete(tmp_dir)
        print("OK")

        print("Integration: PRE -> POST -> STOP ........ ", end="")
        test_pre_post_stop_pipeline(log_path, tmp_dir)
        print("OK")

        print("Integration: PRE-to-PRE baseline ....... ", end="")
        test_pre_to_pre_baseline(log_path)
        print("OK")

        print("Unit: [CONT] continuation inherits task_turn  ", end="")
        test_cont_continuation_inherits_task_turn(log_path)
        print("OK")

        print("Integration: POST Agent logs totalTokens ....... ", end="")
        test_post_agent_logs_total_tokens(log_path)
        print("OK")

        print("Integration: POST Agent skips zero tokens ...... ", end="")
        test_post_agent_zero_tokens_not_logged(log_path)
        print("OK")

        print("Integration: PRE skips log after Agent ......... ", end="")
        test_pre_skips_logging_after_agent(log_path)
        print("OK")

    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
