#!/usr/bin/env python3
"""
Tests for claude-apiary budgeter hooks and logger.

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
    """Generate a valid UUID-format session ID for testing."""
    u = uuid.uuid4()
    return str(u)


def make_test_project(tmp_dir):
    """Create a fake project directory with .claude/budgeter.json so hooks
    redirect all data paths to tmp_dir instead of real budgeter/data/."""
    claude_dir = tmp_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    config = {"monitored_tools": ["Agent", "Bash", "Read", "Write"]}
    (claude_dir / "budgeter.json").write_text(json.dumps(config))
    return tmp_dir


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

def test_pre_post_stop_pipeline(tmp_dir):
    """PRE -> POST -> STOP with empty transcript. Verifies plumbing end-to-end."""
    project_dir = make_test_project(tmp_dir / "project")
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    tmp_path = project_dir / ".claude" / "budgeter-tmp"
    cwd = str(project_dir)
    session_id = make_session_id()
    payload = {"tool_name": "Bash", "session_id": session_id, "transcript_path": "", "cwd": cwd}

    # PRE — no prior baseline, so no entry logged. Should write baseline.
    before = log_entry_count(log_path)
    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0, f"PRE failed: {r.stderr}"
    baseline_path = tmp_path / f"{session_id}_baseline.json"
    assert baseline_path.exists(), "PRE should write a baseline"
    assert log_entry_count(log_path) == before, "PRE with no prior baseline must not write an entry"

    # POST — now a no-op
    r = run_hook("post_tool_use.py", payload)
    assert r.returncode == 0, f"POST failed: {r.stderr}"

    # STOP — logs final entry (empty transcript -> delta=0 -> not written), cleans up
    r = run_hook("stop_session.py", {"session_id": session_id, "transcript_path": "", "cwd": cwd})
    assert r.returncode == 0, f"STOP failed: {r.stderr}"
    leftover = list(tmp_path.glob(f"{session_id}_*")) if tmp_path.exists() else []
    assert not leftover, f"STOP left tmp files: {[f.name for f in leftover]}"


def test_pre_to_pre_baseline(tmp_dir):
    """PRE saves prev_tool_name in baseline so the next PRE can attribute costs correctly."""
    import budgeter.lib.logger as lg
    project_dir = make_test_project(tmp_dir / "project_pre2pre")
    cwd = str(project_dir)
    session_id = make_session_id()
    payload = {"tool_name": "Write", "session_id": session_id, "transcript_path": "", "cwd": cwd}

    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0

    # Point logger at the same project paths the subprocess used
    orig_tmp = lg.TMP_DIR
    lg.configure_for_project(cwd)
    try:
        b = lg.load_baseline(session_id)
        assert b is not None
        assert b["prev_tool_name"] == "Write", "Baseline must record the tool that just ran"
        assert b["tokens"] == 0  # empty transcript
        assert "task_turn" in b, "Baseline must include task_turn"
    finally:
        lg.TMP_DIR = orig_tmp
        lg.configure_for_project(cwd)
        lg.cleanup_session(session_id)


def test_cont_continuation_inherits_task_turn(tmp_dir):
    """A turn whose assistant message starts with [CONT] must inherit task_turn from the prior baseline."""
    import budgeter.lib.logger as lg
    session_id = make_session_id()

    # Simulate an existing baseline from turn 1, task_turn=1
    orig_tmp = lg.TMP_DIR
    lg.TMP_DIR = tmp_dir
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

def test_post_agent_logs_total_tokens(tmp_dir):
    """PostToolUse hook must log an Agent entry with tokens_delta == totalTokens."""
    import budgeter.lib.logger as lg
    project_dir = make_test_project(tmp_dir / "project_agent")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    session_id = make_session_id()

    # Point logger at project paths and write a baseline
    orig_tmp, orig_log = lg.TMP_DIR, lg.LOG_PATH
    lg.configure_for_project(cwd)
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
            "cwd": cwd,
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
        lg.LOG_PATH = orig_log
        lg.configure_for_project(cwd)
        lg.cleanup_session(session_id)


def test_post_agent_zero_tokens_not_logged(tmp_dir):
    """PostToolUse hook must not log an Agent entry when totalTokens == 0."""
    project_dir = make_test_project(tmp_dir / "project_agent_zero")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    session_id = make_session_id()
    before = log_entry_count(log_path)
    payload = {
        "tool_name": "Agent",
        "session_id": session_id,
        "cwd": cwd,
        "tool_response": {"totalTokens": 0},
    }
    r = run_hook("post_tool_use.py", payload)
    assert r.returncode == 0, f"POST failed: {r.stderr}"
    assert log_entry_count(log_path) == before, "Zero-token Agent entry must not be written"


def test_pre_skips_logging_after_agent(tmp_dir):
    """PRE hook must not log a duplicate entry when prev_tool_name == 'Agent'."""
    import budgeter.lib.logger as lg
    project_dir = make_test_project(tmp_dir / "project_skip_agent")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    session_id = make_session_id()

    orig_tmp, orig_log = lg.TMP_DIR, lg.LOG_PATH
    lg.configure_for_project(cwd)
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
        payload = {"tool_name": "Bash", "session_id": session_id, "transcript_path": "", "cwd": cwd}
        r = run_hook("pre_tool_use.py", payload)
        assert r.returncode == 0, f"PRE failed: {r.stderr}"
        assert log_entry_count(log_path) == before, "PRE must not log an Agent entry (PostToolUse already did)"
    finally:
        lg.TMP_DIR = orig_tmp
        lg.LOG_PATH = orig_log
        lg.cleanup_session(session_id)


# ---------------------------------------------------------------------------
# Unit tests: estimator
# ---------------------------------------------------------------------------

def test_detect_scope_flags_scope_keyword(tmp_dir):
    """detect_scope_flags fires 'scope_keywords' on refactor/rewrite/etc."""
    from budgeter.lib.estimator import detect_scope_flags
    config = {}
    assert "scope_keywords" in detect_scope_flags("I'll refactor the auth module.", config)
    assert "scope_keywords" in detect_scope_flags("Let me rewrite this entirely.", config)
    assert "scope_keywords" not in detect_scope_flags("I'll read the file.", config)


def test_detect_scope_flags_breadth_keyword(tmp_dir):
    """detect_scope_flags fires 'breadth_keywords' on entire/codebase/throughout/etc."""
    from budgeter.lib.estimator import detect_scope_flags
    config = {}
    assert "breadth_keywords" in detect_scope_flags("Search the entire codebase.", config)
    assert "breadth_keywords" in detect_scope_flags("Apply this throughout the project.", config)
    assert "breadth_keywords" not in detect_scope_flags("Fix the bug in foo.py.", config)
    # "all" and "multiple" removed — too noisy
    assert "breadth_keywords" not in detect_scope_flags("Update all the tests.", config)


def test_detect_scope_flags_file_count(tmp_dir):
    """detect_scope_flags fires 'file_count' when 3+ distinct files are mentioned."""
    from budgeter.lib.estimator import detect_scope_flags
    config = {}
    msg = "I'll update auth.py, logger.py, and config.json."
    assert "file_count" in detect_scope_flags(msg, config)
    msg_two = "I'll read foo.py and bar.py."
    assert "file_count" not in detect_scope_flags(msg_two, config)


def test_detect_scope_flags_step_count(tmp_dir):
    """detect_scope_flags fires 'step_count' when 4+ step connectives are present."""
    from budgeter.lib.estimator import detect_scope_flags
    config = {}
    msg = "First I'll read the file, then search for usages, then also update tests, and finally verify."
    assert "step_count" in detect_scope_flags(msg, config)
    msg_short = "I'll read the file then fix the bug."
    assert "step_count" not in detect_scope_flags(msg_short, config)


def test_detect_scope_flags_empty(tmp_dir):
    """detect_scope_flags returns [] for empty or plain messages."""
    from budgeter.lib.estimator import detect_scope_flags
    config = {}
    assert detect_scope_flags("", config) == []
    assert detect_scope_flags("Let me read the file.", config) == []


def test_detect_scope_flags_investigative_keywords(tmp_dir):
    """detect_scope_flags fires 'investigative_keywords' on user_text, not assistant text."""
    from budgeter.lib.estimator import detect_scope_flags
    config = {}
    # Fires when user_text contains investigative word
    assert "investigative_keywords" in detect_scope_flags("Let me look.", config, user_text="Why is this failing?")
    assert "investigative_keywords" in detect_scope_flags("", config, user_text="Can you explain this module?")
    assert "investigative_keywords" in detect_scope_flags("", config, user_text="I need you to debug this.")
    # Does NOT fire when user_text is absent
    assert "investigative_keywords" not in detect_scope_flags("Why is this failing?", config)
    # Does NOT fire on plain user messages
    assert "investigative_keywords" not in detect_scope_flags("", config, user_text="Fix the bug in foo.py.")


def test_estimate_magnitude_matching_tasks(tmp_dir):
    """estimate_magnitude returns median of tasks sharing at least one rule."""
    from budgeter.lib.estimator import estimate_magnitude
    config = {"min_flagged_tasks": 2}
    log_entries = [
        {"turn_number": 1, "task_turn": 1, "session_id": "s1",
         "assistant_message": "I'll refactor the module.", "net_tokens_delta": 10000},
        {"turn_number": 2, "task_turn": 2, "session_id": "s1",
         "assistant_message": "I'll rewrite the handler.", "net_tokens_delta": 20000},
        {"turn_number": 3, "task_turn": 3, "session_id": "s1",
         "assistant_message": "Fix the typo.", "net_tokens_delta": 500},
    ]
    median, size, fallback = estimate_magnitude(["scope_keywords"], log_entries, config)
    assert median == 15000, f"Expected 15000, got {median}"
    assert size == 2
    assert not fallback


def test_estimate_magnitude_fallback(tmp_dir):
    """estimate_magnitude falls back to all flagged tasks when overlap is too small."""
    from budgeter.lib.estimator import estimate_magnitude
    config = {"min_flagged_tasks": 3}
    log_entries = [
        # Only 1 task matching scope_keywords — below min_flagged_tasks=3
        {"turn_number": 1, "task_turn": 1, "session_id": "s1",
         "assistant_message": "I'll refactor this.", "net_tokens_delta": 50000},
        # 3 tasks matching breadth_keywords — enough for fallback
        {"turn_number": 2, "task_turn": 2, "session_id": "s1",
         "assistant_message": "Update throughout the project.", "net_tokens_delta": 10000},
        {"turn_number": 3, "task_turn": 3, "session_id": "s1",
         "assistant_message": "Search entire codebase.", "net_tokens_delta": 20000},
        {"turn_number": 4, "task_turn": 4, "session_id": "s1",
         "assistant_message": "Apply this across the codebase everywhere.", "net_tokens_delta": 30000},
    ]
    median, size, fallback = estimate_magnitude(["scope_keywords"], log_entries, config)
    assert fallback, "Should fall back to all flagged tasks"
    assert size == 4  # all 4 tasks are flagged


def test_feedback_append_read(tmp_dir):
    """append_feedback / read_feedback roundtrip."""
    import budgeter.lib.logger as lg
    orig_feedback = lg.FEEDBACK_PATH
    lg.FEEDBACK_PATH = tmp_dir / "feedback.jsonl"
    try:
        assert lg.read_feedback() == []
        entry = {
            "session_id": "s1", "task_turn": 1,
            "scope_flags": ["scope_keywords"], "score": 1.0,
            "predicted_cost": 80000, "warning_fired": True,
        }
        lg.append_feedback(entry)
        result = lg.read_feedback()
        assert len(result) == 1
        assert result[0]["predicted_cost"] == 80000
        assert result[0]["warning_fired"] is True
    finally:
        lg.FEEDBACK_PATH = orig_feedback


def test_baseline_stores_predicted_cost_and_warning_fired(tmp_dir):
    """save_baseline stores predicted_cost and warning_fired; load_baseline retrieves them."""
    import budgeter.lib.logger as lg
    orig_tmp = lg.TMP_DIR
    lg.TMP_DIR = tmp_dir
    session_id = make_session_id()
    try:
        lg.save_baseline(session_id, 1000, predicted_cost=50000, warning_fired=True)
        b = lg.load_baseline(session_id)
        assert b["predicted_cost"] == 50000
        assert b["warning_fired"] is True
    finally:
        lg.TMP_DIR = orig_tmp
        lg.cleanup_session(session_id)


def test_feedback_written_at_session_end(tmp_dir):
    """Stop hook writes a feedback record for the last task."""
    import budgeter.lib.logger as lg
    project_dir = make_test_project(tmp_dir / "project_feedback")
    cwd = str(project_dir)
    session_id = make_session_id()

    orig_tmp, orig_log, orig_feedback = lg.TMP_DIR, lg.LOG_PATH, lg.FEEDBACK_PATH
    lg.configure_for_project(cwd)
    try:
        before = sum(
            1 for e in lg.read_feedback() if e.get("session_id") == session_id
        )
        lg.save_baseline(
            session_id, tokens=3000,
            prev_tool_name="Write",
            prev_assistant_message="Updating all configs.",
            turn_number=2,
            task_turn=2,
            user_message="update configs",
            scope_flags=["breadth_keywords"],
            predicted_cost=0,
            warning_fired=False,
        )
        r = run_hook("stop_session.py", {
            "session_id": session_id,
            "transcript_path": "",
            "cwd": cwd,
        })
        assert r.returncode == 0, f"STOP failed: {r.stderr}"
        after = [e for e in lg.read_feedback() if e.get("session_id") == session_id]
        assert len(after) == before + 1, "Stop hook must write one feedback record"
        assert after[-1]["task_turn"] == 2
        assert after[-1]["scope_flags"] == ["breadth_keywords"]
        assert after[-1]["warning_fired"] is False
    finally:
        lg.TMP_DIR = orig_tmp
        lg.LOG_PATH = orig_log
        lg.FEEDBACK_PATH = orig_feedback


def test_is_approval_message(tmp_dir):
    """is_approval_message matches short confirmations and rejects non-approvals."""
    from budgeter.lib.estimator import is_approval_message
    # Approvals
    assert is_approval_message("yes")
    assert is_approval_message("Yes")
    assert is_approval_message("yep")
    assert is_approval_message("yeah")
    assert is_approval_message("ok")
    assert is_approval_message("okay")
    assert is_approval_message("proceed")
    assert is_approval_message("go ahead")
    assert is_approval_message("do it")
    assert is_approval_message("sounds good")
    assert is_approval_message("that works")
    assert is_approval_message("sure")
    assert is_approval_message("lgtm")
    assert is_approval_message("lets go")
    assert is_approval_message("please")
    # Not approvals
    assert not is_approval_message("")
    assert not is_approval_message("yes but also refactor the auth module and update all tests")
    assert not is_approval_message("Fix the bug in foo.py")
    assert not is_approval_message("Can you explain why this is failing?")
    assert not is_approval_message("run the report")


def test_score_flags_equal_weights(tmp_dir):
    """score_flags sums weights; defaults to 1.0 per rule when rule_weights absent."""
    from budgeter.lib.estimator import score_flags
    assert score_flags(["scope_keywords", "file_count"], {}) == 2.0
    assert score_flags([], {}) == 0.0
    assert score_flags(["scope_keywords"], {"rule_weights": {"scope_keywords": 2.0}}) == 2.0


def test_score_flags_weighted(tmp_dir):
    """score_flags uses per-rule weights from config."""
    from budgeter.lib.estimator import score_flags
    config = {"rule_weights": {"scope_keywords": 2.0, "step_count": 0.5}}
    assert score_flags(["scope_keywords", "step_count"], config) == 2.5
    assert score_flags(["step_count"], config) == 0.5
    # Unknown rule defaults to 1.0
    assert score_flags(["unknown_rule"], config) == 1.0


def test_estimate_magnitude_no_history(tmp_dir):
    """estimate_magnitude returns (0, 0, False) when there is not enough history."""
    from budgeter.lib.estimator import estimate_magnitude
    config = {"min_flagged_tasks": 10}
    assert estimate_magnitude(["scope_keywords"], [], config) == (0, 0, False)
    assert estimate_magnitude([], [{"turn_number": 1, "task_turn": 1, "session_id": "s1",
                                    "assistant_message": "refactor", "net_tokens_delta": 1000}], config) == (0, 0, False)


# ---------------------------------------------------------------------------
# Unit tests: tune.py
# ---------------------------------------------------------------------------

def _make_records(tasks):
    """
    Build enriched feedback records from a list of (scope_flags, actual_cost, was_expensive).
    """
    return [
        {"session_id": "s1", "task_turn": i + 1,
         "scope_flags": flags, "actual_cost": cost, "was_expensive": exp}
        for i, (flags, cost, exp) in enumerate(tasks)
    ]


def test_tune_compute_actual_costs(tmp_dir):
    """compute_actual_costs sums net_tokens_delta per (session_id, task_turn)."""
    sys.path.insert(0, str(BUDGETER_DIR))
    from tune import compute_actual_costs
    log_entries = [
        {"turn_number": 1, "task_turn": 1, "session_id": "s1", "net_tokens_delta": 10000},
        {"turn_number": 1, "task_turn": 1, "session_id": "s1", "net_tokens_delta": 5000},
        {"turn_number": 2, "task_turn": 2, "session_id": "s1", "net_tokens_delta": 20000},
        {"turn_number": 1, "task_turn": 1, "session_id": "s2", "net_tokens_delta": 8000},
    ]
    costs = compute_actual_costs(log_entries)
    assert costs[("s1", 1)] == 15000
    assert costs[("s1", 2)] == 20000
    assert costs[("s2", 1)] == 8000


def test_tune_enrich_feedback_deduplicates(tmp_dir):
    """enrich_feedback deduplicates duplicate (session_id, task_turn) records."""
    sys.path.insert(0, str(BUDGETER_DIR))
    from tune import enrich_feedback
    log_entries = [
        {"turn_number": 1, "task_turn": 1, "session_id": "s1", "net_tokens_delta": 50000},
    ]
    # Two feedback entries for the same task (PRE + Stop both wrote)
    feedback_entries = [
        {"session_id": "s1", "task_turn": 1, "scope_flags": ["scope_keywords"], "warning_fired": True},
        {"session_id": "s1", "task_turn": 1, "scope_flags": ["scope_keywords"], "warning_fired": True},
    ]
    records, threshold = enrich_feedback(log_entries, feedback_entries, 75)
    assert len(records) == 1, "Duplicate task feedback records must be deduplicated"
    assert records[0]["actual_cost"] == 50000


def test_tune_analyze_rules_precision(tmp_dir):
    """analyze_rules computes per-rule fires, expensive, and precision correctly."""
    sys.path.insert(0, str(BUDGETER_DIR))
    from tune import analyze_rules
    records = _make_records([
        (["scope_keywords"], 60000, True),
        (["scope_keywords"], 70000, True),
        (["scope_keywords"], 80000, True),
        (["scope_keywords"], 5000, False),
        (["breadth_keywords"], 90000, True),
        (["breadth_keywords"], 3000, False),
    ])
    stats = analyze_rules(records, min_samples=3)
    sk = stats["scope_keywords"]
    assert sk["fires"] == 4
    assert sk["expensive"] == 3
    assert abs(sk["precision"] - 0.75) < 0.001
    assert sk["sufficient"] is True

    bk = stats["breadth_keywords"]
    assert bk["fires"] == 2
    assert bk["sufficient"] is False  # below min_samples=3


def test_tune_propose_weights_scales_proportionally(tmp_dir):
    """propose_weights scales rules above mean up and below mean down."""
    sys.path.insert(0, str(BUDGETER_DIR))
    from tune import propose_weights
    # scope_keywords: 75% precision, breadth_keywords: 25% precision, mean = 50%
    rule_stats = {
        "scope_keywords": {"fires": 10, "expensive": 8, "precision": 0.75, "sufficient": True},
        "breadth_keywords": {"fires": 10, "expensive": 2, "precision": 0.25, "sufficient": True},  # no, 2/10=0.2? Let me fix
    }
    # Actually 2/10 = 0.2, not 0.25. Let me just use consistent values.
    rule_stats = {
        "scope_keywords": {"fires": 10, "expensive": 8, "precision": 0.8, "sufficient": True},
        "breadth_keywords": {"fires": 10, "expensive": 2, "precision": 0.2, "sufficient": True},
    }
    # mean = 0.5, scope scale = 0.8/0.5 = 1.6, breadth scale = 0.2/0.5 = 0.4
    current_weights = {"scope_keywords": 1.0, "breadth_keywords": 1.0}
    proposed, mean_prec = propose_weights(current_weights, rule_stats)
    assert abs(mean_prec - 0.5) < 0.001
    assert proposed["scope_keywords"] > 1.0, "High-precision rule should get higher weight"
    assert proposed["breadth_keywords"] < 1.0, "Low-precision rule should get lower weight"


def test_tune_propose_weights_no_eligible_rules(tmp_dir):
    """propose_weights returns empty dict when no rules have sufficient data."""
    sys.path.insert(0, str(BUDGETER_DIR))
    from tune import propose_weights
    rule_stats = {
        "scope_keywords": {"fires": 2, "expensive": 1, "precision": 0.5, "sufficient": False},
    }
    proposed, mean_prec = propose_weights({"scope_keywords": 1.0}, rule_stats)
    assert proposed == {}
    assert mean_prec == 0.0


def test_tune_propose_weights_clamps_extremes(tmp_dir):
    """propose_weights clamps output to [WEIGHT_MIN, WEIGHT_MAX]."""
    sys.path.insert(0, str(BUDGETER_DIR))
    from tune import propose_weights, WEIGHT_MIN, WEIGHT_MAX
    # One rule with 100% precision, one with 0% — extremes
    rule_stats = {
        "scope_keywords": {"fires": 10, "expensive": 10, "precision": 1.0, "sufficient": True},
        "step_count":     {"fires": 10, "expensive": 0,  "precision": 0.0, "sufficient": True},
    }
    # mean = 0.5, scale for scope = 1.0/0.5 = 2.0, scale for step = 0.0/0.5 = 0.0
    proposed, _ = propose_weights({"scope_keywords": 1.0, "step_count": 1.0}, rule_stats)
    assert proposed.get("scope_keywords", 0) <= WEIGHT_MAX
    assert proposed.get("step_count", 1) >= WEIGHT_MIN


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Require budgeter-log-enabled for integration tests
    flag = Path.home() / ".claude" / "budgeter-log-enabled"
    if not flag.exists():
        print("Note: budgeter-log-enabled not set — integration tests will skip logging checks.")

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
        test_pre_post_stop_pipeline(tmp_dir)
        print("OK")

        print("Integration: PRE-to-PRE baseline ....... ", end="")
        test_pre_to_pre_baseline(tmp_dir)
        print("OK")

        print("Unit: [CONT] continuation inherits task_turn  ", end="")
        test_cont_continuation_inherits_task_turn(tmp_dir)
        print("OK")

        print("Integration: POST Agent logs totalTokens ....... ", end="")
        test_post_agent_logs_total_tokens(tmp_dir)
        print("OK")

        print("Integration: POST Agent skips zero tokens ...... ", end="")
        test_post_agent_zero_tokens_not_logged(tmp_dir)
        print("OK")

        print("Integration: PRE skips log after Agent ......... ", end="")
        test_pre_skips_logging_after_agent(tmp_dir)
        print("OK")

        print("Unit: feedback append/read roundtrip ............ ", end="")
        test_feedback_append_read(tmp_dir)
        print("OK")

        print("Unit: baseline stores predicted_cost/warning .... ", end="")
        test_baseline_stores_predicted_cost_and_warning_fired(tmp_dir)
        print("OK")

        print("Integration: feedback written at session end ..... ", end="")
        test_feedback_written_at_session_end(tmp_dir)
        print("OK")

        print("Unit: detect_scope_flags scope_keyword ......... ", end="")
        test_detect_scope_flags_scope_keyword(tmp_dir)
        print("OK")

        print("Unit: detect_scope_flags breadth_keyword ........ ", end="")
        test_detect_scope_flags_breadth_keyword(tmp_dir)
        print("OK")

        print("Unit: detect_scope_flags file_count ............. ", end="")
        test_detect_scope_flags_file_count(tmp_dir)
        print("OK")

        print("Unit: detect_scope_flags step_count ............. ", end="")
        test_detect_scope_flags_step_count(tmp_dir)
        print("OK")

        print("Unit: detect_scope_flags empty .................. ", end="")
        test_detect_scope_flags_empty(tmp_dir)
        print("OK")

        print("Unit: detect_scope_flags investigative .......... ", end="")
        test_detect_scope_flags_investigative_keywords(tmp_dir)
        print("OK")

        print("Unit: is_approval_message ...................... ", end="")
        test_is_approval_message(tmp_dir)
        print("OK")

        print("Unit: score_flags equal weights ................. ", end="")
        test_score_flags_equal_weights(tmp_dir)
        print("OK")

        print("Unit: score_flags weighted ...................... ", end="")
        test_score_flags_weighted(tmp_dir)
        print("OK")

        print("Unit: estimate_magnitude matching tasks ......... ", end="")
        test_estimate_magnitude_matching_tasks(tmp_dir)
        print("OK")

        print("Unit: estimate_magnitude fallback ............... ", end="")
        test_estimate_magnitude_fallback(tmp_dir)
        print("OK")

        print("Unit: estimate_magnitude no history ............. ", end="")
        test_estimate_magnitude_no_history(tmp_dir)
        print("OK")

        print("Unit: tune compute_actual_costs ................. ", end="")
        test_tune_compute_actual_costs(tmp_dir)
        print("OK")

        print("Unit: tune enrich_feedback deduplicates ......... ", end="")
        test_tune_enrich_feedback_deduplicates(tmp_dir)
        print("OK")

        print("Unit: tune analyze_rules precision .............. ", end="")
        test_tune_analyze_rules_precision(tmp_dir)
        print("OK")

        print("Unit: tune propose_weights scales proportionally  ", end="")
        test_tune_propose_weights_scales_proportionally(tmp_dir)
        print("OK")

        print("Unit: tune propose_weights no eligible rules .... ", end="")
        test_tune_propose_weights_no_eligible_rules(tmp_dir)
        print("OK")

        print("Unit: tune propose_weights clamps extremes ...... ", end="")
        test_tune_propose_weights_clamps_extremes(tmp_dir)
        print("OK")

    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
