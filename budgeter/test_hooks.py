#!/usr/bin/env python3
"""
Tests for claude-apiary budgeter hooks and logger.

Covers:
  - Unit tests for logger (append_entry zero-delta filter, baseline round-trip)
  - Integration test: PRE -> POST -> STOP sequence
  - PRE-to-PRE baseline: PRE saves prev_tool_name for next PRE to attribute correctly
  - count_tasks: counts unique (session_id, task_turn) pairs, not raw entries
  - Agent PostToolUse: logs Agent token cost from tool_response.totalTokens
  - No double-count: PRE skips logging when prev_tool_name == "Agent"
  - Session-length nudge tiers, and its one-shot behaviour in the PRE hook
  - Reading log entries written before the warning feature was deleted
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

BUDGETER_DIR = Path(__file__).parent
APIS_DIR = BUDGETER_DIR.parent
HOOKS_DIR = BUDGETER_DIR / "hooks"
PYTHON = sys.executable

# Enforce that tests never write to real budgeter/data paths. Any call to
# append_entry / save_baseline targeting the default production paths will
# raise while this flag is set.
os.environ["APIARY_BUDGETER_TEST_ISOLATION"] = "1"

sys.path.insert(0, str(APIS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_session_id():
    """Generate a valid UUID-format session ID for testing."""
    u = uuid.uuid4()
    return str(u)


def make_test_project(tmp_path):
    """Create a fake project directory with .claude/budgeter.json so hooks
    redirect all data paths to tmp_path instead of real budgeter/data/."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    config = {"monitored_tools": ["Agent", "Bash", "Read", "Write"]}
    (claude_dir / "budgeter.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def run_hook(script, payload, project=None, env_extra=None):
    """Run a budgeter hook as a subprocess against a throwaway project.

    ``APIARY_TARGET_REPO`` is pinned to that project (the payload's ``cwd``
    unless *project* overrides it) and ``CLAUDE_PROJECT_DIR`` — which
    ``core.flags`` consults *first* — is dropped, so both the feature flags
    the hook reads and the per-session flag files it writes resolve inside
    the test project. Without that the hooks read the developer's own
    ``budgeter-log-enabled`` state out of this checkout and wrote session
    flag files into it (T-2026-274).
    """
    env = {**os.environ, "APIARY_BUDGETER_TEST_ISOLATION": "1"}
    env.pop("CLAUDE_PROJECT_DIR", None)
    target = str(project) if project is not None else payload.get("cwd", "")
    if target:
        env["APIARY_TARGET_REPO"] = target
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PYTHON, str(HOOKS_DIR / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )


def flag_file(project, flag_name):
    """The sentinel ``core.flags`` looks for inside *project*."""
    from core import flags

    return Path(project) / flags.PIN_FLAGS_SUBPATH / f"{flag_name}-enabled"


def _with_flag_enabled(flag_name, project):
    """Enable a budgeter flag for the throwaway *project*, never for the
    checkout the suite runs from. Returns a cleanup callable."""
    path = flag_file(project, flag_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("enabled", encoding="utf-8")
    return lambda: path.unlink(missing_ok=True)


def log_entry_count(log_path):
    if not log_path.exists():
        return 0
    return sum(1 for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip())


# ---------------------------------------------------------------------------
# Unit tests: logger
# ---------------------------------------------------------------------------


def test_isolation_guard_blocks_default_paths(tmp_path):
    """With APIARY_BUDGETER_TEST_ISOLATION=1, writing to the default production
    LOG_PATH / TMP_DIR must raise. Protects the suite from tests that forget
    configure_for_project or direct-patching."""
    import budgeter.lib.logger as lg

    orig_log, orig_tmp = lg.LOG_PATH, lg.TMP_DIR
    try:
        lg.LOG_PATH = lg._DEFAULT_LOG_PATH
        try:
            lg.append_entry({"tokens_delta": 100, "tool_name": "Bash"})
            raise AssertionError("append_entry should have raised on default LOG_PATH")
        except RuntimeError:
            pass

        lg.TMP_DIR = lg._DEFAULT_TMP_DIR
        try:
            lg.save_baseline(make_session_id(), tokens=1)
            raise AssertionError("save_baseline should have raised on default TMP_DIR")
        except RuntimeError:
            pass
    finally:
        lg.LOG_PATH = orig_log
        lg.TMP_DIR = orig_tmp


def test_append_entry_skips_zero_delta(tmp_path):
    """append_entry must not write entries with tokens_delta == 0."""
    import budgeter.lib.logger as lg

    orig_log = lg.LOG_PATH
    lg.LOG_PATH = tmp_path / "log.jsonl"
    try:
        lg.append_entry({"tokens_delta": 0, "tool_name": "Bash"})
        assert not lg.LOG_PATH.exists(), "Zero-delta entry should not be written"

        lg.append_entry({"tokens_delta": 100, "tool_name": "Bash"})
        assert lg.LOG_PATH.exists(), "Non-zero entry should be written"
        assert log_entry_count(lg.LOG_PATH) == 1
    finally:
        lg.LOG_PATH = orig_log


def test_count_tasks(tmp_path):
    """count_tasks must count unique (session_id, task_turn) pairs, not raw entries."""
    import budgeter.lib.logger as lg

    orig_log = lg.LOG_PATH
    lg.LOG_PATH = tmp_path / "count_tasks_log.jsonl"
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


def test_baseline_save_load_delete(tmp_path):
    """Baseline round-trip: save -> load -> verify tokens and task_turn -> cleanup removes it."""
    import budgeter.lib.logger as lg

    orig_tmp = lg.TMP_DIR
    lg.TMP_DIR = tmp_path
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


# ---------------------------------------------------------------------------
# Integration tests: hook sequence
# ---------------------------------------------------------------------------


def test_pre_post_stop_sequence(tmp_path):
    """PRE -> POST -> STOP with empty transcript. Verifies plumbing end-to-end."""
    project_dir = make_test_project(tmp_path / "project")
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


def test_pre_to_pre_baseline(tmp_path):
    """PRE saves prev_tool_name in baseline so the next PRE can attribute costs correctly."""
    import budgeter.lib.logger as lg

    project_dir = make_test_project(tmp_path / "project_pre2pre")
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


# ---------------------------------------------------------------------------
# Agent PostToolUse tests
# ---------------------------------------------------------------------------


def test_post_agent_logs_total_tokens(tmp_path):
    """PostToolUse hook must log an Agent entry with tokens_delta == totalTokens."""
    import budgeter.lib.logger as lg

    project_dir = make_test_project(tmp_path / "project_agent")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    session_id = make_session_id()

    _with_flag_enabled("budgeter-log", project_dir)

    # Point logger at project paths and write a baseline
    orig_tmp, orig_log = lg.TMP_DIR, lg.LOG_PATH
    lg.configure_for_project(cwd)
    try:
        lg.save_baseline(
            session_id,
            tokens=5000,
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
        agent_entries = [
            e
            for e in entries
            if e.get("session_id") == session_id and e.get("tool_name") == "Agent"
        ]
        assert len(agent_entries) == 1
        assert agent_entries[0]["tokens_delta"] == 12345
        assert agent_entries[0]["net_tokens_delta"] == 12345
        assert agent_entries[0]["task_turn"] == 3
    finally:
        lg.TMP_DIR = orig_tmp
        lg.LOG_PATH = orig_log
        lg.configure_for_project(cwd)
        lg.cleanup_session(session_id)


def test_post_agent_zero_tokens_not_logged(tmp_path):
    """PostToolUse hook must not log an Agent entry when totalTokens == 0."""
    project_dir = make_test_project(tmp_path / "project_agent_zero")
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


def test_pre_skips_logging_after_agent(tmp_path):
    """PRE hook must not log a duplicate entry when prev_tool_name == 'Agent'."""
    import budgeter.lib.logger as lg

    project_dir = make_test_project(tmp_path / "project_skip_agent")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    session_id = make_session_id()

    _with_flag_enabled("budgeter-log", project_dir)

    orig_tmp, orig_log = lg.TMP_DIR, lg.LOG_PATH
    lg.configure_for_project(cwd)
    try:
        # Simulate baseline left by Agent's PRE hook
        lg.save_baseline(
            session_id,
            tokens=10000,
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
        assert log_entry_count(log_path) == before, (
            "PRE must not log an Agent entry (PostToolUse already did)"
        )
    finally:
        lg.TMP_DIR = orig_tmp
        lg.LOG_PATH = orig_log
        lg.cleanup_session(session_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    # Every test builds its own throwaway project and enables the flags it
    # needs there, so nothing depends on this checkout's flag state.
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td).resolve()

        print("Unit: isolation guard blocks default paths ", end="")
        test_isolation_guard_blocks_default_paths(tmp_path)
        print("OK")

        print("Unit: append_entry skips zero-delta ... ", end="")
        test_append_entry_skips_zero_delta(tmp_path)
        print("OK")

        print("Unit: count_tasks deduplicates ......... ", end="")
        test_count_tasks(tmp_path)
        print("OK")

        print("Unit: baseline save/load/delete ....... ", end="")
        test_baseline_save_load_delete(tmp_path)
        print("OK")

        print("Integration: PRE -> POST -> STOP ........ ", end="")
        test_pre_post_stop_sequence(tmp_path)
        print("OK")

        print("Integration: PRE-to-PRE baseline ....... ", end="")
        test_pre_to_pre_baseline(tmp_path)
        print("OK")

        print("Integration: POST Agent logs totalTokens ....... ", end="")
        test_post_agent_logs_total_tokens(tmp_path)
        print("OK")

        print("Integration: POST Agent skips zero tokens ...... ", end="")
        test_post_agent_zero_tokens_not_logged(tmp_path)
        print("OK")

        print("Integration: PRE skips log after Agent ......... ", end="")
        test_pre_skips_logging_after_agent(tmp_path)
        print("OK")

        print("Unit: session_length_nudge below threshold ...... ", end="")
        test_session_length_nudge_below_threshold(tmp_path)
        print("OK")

        print("Unit: session_length_nudge soft tier ............ ", end="")
        test_session_length_nudge_soft_tier(tmp_path)
        print("OK")

        print("Unit: session_length_nudge hard tier ............ ", end="")
        test_session_length_nudge_hard_tier(tmp_path)
        print("OK")

        print("Unit: session_length_nudge config override ...... ", end="")
        test_session_length_nudge_config_override(tmp_path)
        print("OK")

        print("Integration: session nudge fires once per tier .. ", end="")
        test_session_length_nudge_fires_once_per_tier(tmp_path)
        print("OK")

        print("Integration: session nudge hard tier fires ...... ", end="")
        test_session_length_nudge_hard_tier_fires(tmp_path)
        print("OK")

        print("Integration: session nudge skipped if detached .. ", end="")
        test_session_length_nudge_skipped_when_detached(tmp_path)
        print("OK")

        print("Integration: session nudge skipped if session-warn off ", end="")
        test_session_length_nudge_skipped_when_session_warn_disabled(tmp_path)
        print("OK")

        print("Unit: cumulative tokens dedupe + cache creation . ", end="")
        test_cumulative_tokens_dedupes_by_message_id_and_counts_cache_creation(tmp_path)
        print("OK")

        print("Unit: baseline atomic save / corrupt load ....... ", end="")
        test_save_baseline_is_atomic_and_load_survives_corruption(tmp_path)
        print("OK")

        print("Integration: corrupt baseline does not wedge .... ", end="")
        test_corrupt_baseline_does_not_wedge_the_session(tmp_path)
        print("OK")

        print("Integration: PRE skips phantom entries .......... ", end="")
        test_pre_skips_phantom_entry_when_no_api_call(tmp_path)
        print("OK")

        print("Integration: POST logs >64KB Agent payload ...... ", end="")
        test_post_agent_payload_over_64kb_is_logged(tmp_path)
        print("OK")

        print("Unit: weighted_delta counts cache creation ...... ", end="")
        test_weighted_delta_counts_cache_creation(tmp_path)
        print("OK")

        print("Integration: old-schema baseline not compared ... ", end="")
        test_old_schema_baseline_is_not_compared(tmp_path)
        print("OK")

        print("Unit: report reads pre-deletion log entries ..... ", end="")
        test_report_reads_entries_written_before_the_warning_feature(tmp_path)
        print("OK")

        print("Unit: warning subsystem is really gone .......... ", end="")
        test_estimator_exposes_only_the_session_nudge(tmp_path)
        print("OK")

    print("\nAll tests passed.")


# ---------------------------------------------------------------------------
# Unit tests: session_length_nudge
# ---------------------------------------------------------------------------


def test_session_length_nudge_below_threshold(tmp_path):
    """Below soft threshold returns (None, None)."""
    from budgeter.lib.estimator import session_length_nudge

    tier, msg = session_length_nudge(0, {})
    assert tier is None and msg is None
    tier, msg = session_length_nudge(599_999, {})
    assert tier is None and msg is None


def test_session_length_nudge_soft_tier(tmp_path):
    """At/above soft threshold returns soft tier."""
    from budgeter.lib.estimator import session_length_nudge

    tier, msg = session_length_nudge(600_000, {})
    assert tier == "soft"
    assert "getting long" in msg
    assert "600,000" in msg


def test_session_length_nudge_hard_tier(tmp_path):
    """At/above hard threshold returns hard tier."""
    from budgeter.lib.estimator import session_length_nudge

    tier, msg = session_length_nudge(800_000, {})
    assert tier == "hard"
    assert "very long" in msg
    assert "800,000" in msg


def test_session_length_nudge_config_override(tmp_path):
    """Custom thresholds in config are honored."""
    from budgeter.lib.estimator import session_length_nudge

    cfg = {"session_warn_soft_tokens": 100, "session_warn_hard_tokens": 200}
    assert session_length_nudge(50, cfg) == (None, None)
    assert session_length_nudge(150, cfg)[0] == "soft"
    assert session_length_nudge(250, cfg)[0] == "hard"


# ---------------------------------------------------------------------------
# Integration tests: session-length nudge in pre_tool_use hook
# ---------------------------------------------------------------------------


def _write_transcript(tmp_path, *, input_tokens=0, cache_read=0, output_tokens=0):
    """Write a minimal Claude Code transcript with one assistant usage record."""
    path = tmp_path / "transcript.jsonl"
    entry = {
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
            },
        }
    }
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return path


def _extract_additional_context(hook_stdout):
    """Parse hook stdout JSON and return additionalContext string, or ''."""
    try:
        payload = json.loads(hook_stdout.strip())
        return payload.get("hookSpecificOutput", {}).get("additionalContext", "") or ""
    except json.JSONDecodeError:
        return ""


def test_session_length_nudge_fires_once_per_tier(tmp_path):
    """Soft nudge fires on first PRE over the threshold; re-runs do not re-inject."""
    project_dir = make_test_project(tmp_path / "project_nudge_once")
    cwd = str(project_dir)
    session_id = make_session_id()
    transcript = _write_transcript(tmp_path, input_tokens=600_000)
    payload = {
        "tool_name": "Bash",
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": cwd,
    }

    _with_flag_enabled("budgeter-session-warn", project_dir)
    r1 = run_hook("pre_tool_use.py", payload)
    assert r1.returncode == 0, f"PRE failed: {r1.stderr}"
    ctx1 = _extract_additional_context(r1.stdout)
    assert "Session context is getting long" in ctx1, (
        f"Soft nudge should fire at 600k tokens; got: {ctx1!r}"
    )

    r2 = run_hook("pre_tool_use.py", payload)
    assert r2.returncode == 0, f"PRE #2 failed: {r2.stderr}"
    ctx2 = _extract_additional_context(r2.stdout)
    assert "Session context is getting long" not in ctx2, (
        f"Second PRE must not re-inject soft nudge; got: {ctx2!r}"
    )


def test_session_length_nudge_hard_tier_fires(tmp_path):
    """Hard nudge fires on first PRE over the hard threshold."""
    project_dir = make_test_project(tmp_path / "project_nudge_hard")
    cwd = str(project_dir)
    session_id = make_session_id()
    transcript = _write_transcript(tmp_path, input_tokens=800_000)
    payload = {
        "tool_name": "Bash",
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": cwd,
    }

    _with_flag_enabled("budgeter-session-warn", project_dir)
    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0, f"PRE failed: {r.stderr}"
    ctx = _extract_additional_context(r.stdout)
    assert "Session context is very long" in ctx, (
        f"Hard nudge should fire at 800k tokens; got: {ctx!r}"
    )


def test_session_length_nudge_skipped_when_detached(tmp_path):
    """APIARY_RUNNER_SUBPROCESS=1 suppresses the nudge even over threshold."""
    project_dir = make_test_project(tmp_path / "project_nudge_detached")
    cwd = str(project_dir)
    session_id = make_session_id()
    transcript = _write_transcript(tmp_path, input_tokens=900_000)
    payload = {
        "tool_name": "Bash",
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": cwd,
    }

    _with_flag_enabled("budgeter-session-warn", project_dir)
    r = run_hook("pre_tool_use.py", payload, env_extra={"APIARY_RUNNER_SUBPROCESS": "1"})
    assert r.returncode == 0, f"PRE failed: {r.stderr}"
    ctx = _extract_additional_context(r.stdout)
    assert "Session context" not in ctx, f"Nudge must not fire in detached runner; got: {ctx!r}"


def test_session_length_nudge_skipped_when_session_warn_disabled(tmp_path):
    """budgeter-session-warn flag disabled suppresses the nudge."""
    project_dir = make_test_project(tmp_path / "project_nudge_session_warn_off")
    cwd = str(project_dir)
    session_id = make_session_id()
    transcript = _write_transcript(tmp_path, input_tokens=900_000)
    payload = {
        "tool_name": "Bash",
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": cwd,
    }

    # No flag file in the project => the feature is off, whatever the
    # developer's own checkout has enabled.
    assert not flag_file(project_dir, "budgeter-session-warn").exists()
    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0, f"PRE failed: {r.stderr}"
    ctx = _extract_additional_context(r.stdout)
    assert "Session context" not in ctx, (
        f"Nudge must not fire when session-warn disabled; got: {ctx!r}"
    )


# ---------------------------------------------------------------------------
# Review 2026-08 Phase 0.4: B1 (crash/atomic), B2 (dedupe), B3 (phantoms),
# B5 (cache creation), B6 (stdin cap)
# ---------------------------------------------------------------------------


def _assistant_lines(msg_id, usage, blocks=("text",)):
    """One JSONL line per content block, all sharing message.id and usage —
    exactly how Claude Code writes a multi-block API turn."""
    lines = []
    for block in blocks:
        content = {"type": block, "text": "ok"} if block == "text" else {"type": block}
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": msg_id,
                        "role": "assistant",
                        "content": [content],
                        "usage": usage,
                    },
                }
            )
        )
    return lines


def test_cumulative_tokens_dedupes_by_message_id_and_counts_cache_creation(tmp_path):
    import budgeter.lib.logger as lg

    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 300,
        "output_tokens": 40,
    }
    entries = [
        json.loads(ln) for ln in _assistant_lines("m1", usage, ("thinking", "text", "tool_use"))
    ]
    # Three lines, one API call: 10 + 900 + 300 + 40, not 3x that.
    assert lg.get_cumulative_tokens(entries) == 1250
    # A record with no id is counted on its own.
    entries.append(
        {"message": {"role": "assistant", "usage": {"input_tokens": 5, "output_tokens": 1}}}
    )
    assert lg.get_cumulative_tokens(entries) == 1256
    assert lg.get_last_call_tokens(entries) == (5, 0, 0, 1)
    assert lg.get_last_call_tokens([]) == (0, 0, 0, 0)


def test_save_baseline_is_atomic_and_load_survives_corruption(tmp_path):
    import budgeter.lib.logger as lg

    orig_tmp = lg.TMP_DIR
    lg.TMP_DIR = tmp_path / "tmp"
    sid = make_session_id()
    try:
        lg.save_baseline(sid, tokens=42, prev_tool_name="Bash", baseline_cache_creation=7)
        files = sorted(p.name for p in lg.TMP_DIR.iterdir())
        assert files == [f"{sid}_baseline.json"], f"temp file left behind: {files}"
        b = lg.load_baseline(sid)
        assert b["tokens"] == 42 and b["baseline_cache_creation"] == 7
        # A hook killed mid-write leaves truncated JSON: treated as absent.
        (lg.TMP_DIR / f"{sid}_baseline.json").write_text('{"tokens": 12', encoding="utf-8")
        assert lg.load_baseline(sid) is None
        lg.save_baseline(sid, tokens=43)
        assert lg.load_baseline(sid)["tokens"] == 43
    finally:
        lg.cleanup_session(sid)
        lg.TMP_DIR = orig_tmp


def test_corrupt_baseline_does_not_wedge_the_session(tmp_path):
    """PRE and STOP both used to die on a truncated baseline and, since neither
    rewrote it, every later monitored call errored too."""
    project_dir = make_test_project(tmp_path / "project_corrupt")
    cwd = str(project_dir)
    tmp_dir = project_dir / ".claude" / "budgeter-tmp"
    session_id = make_session_id()
    payload = {"tool_name": "Bash", "session_id": session_id, "transcript_path": "", "cwd": cwd}
    baseline_path = tmp_dir / f"{session_id}_baseline.json"

    tmp_dir.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text('{"tokens": 12', encoding="utf-8")
    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0, f"PRE crashed on corrupt baseline: {r.stderr}"
    assert json.loads(r.stdout.strip()) is not None, "PRE must still print its JSON response"
    assert "permissionDecision" not in r.stdout
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["prev_tool_name"] == "Bash", (
        "PRE must rewrite the baseline"
    )

    baseline_path.write_text('{"tokens": 12', encoding="utf-8")
    _with_flag_enabled("budgeter-log", project_dir)
    r = run_hook("stop_session.py", {"session_id": session_id, "transcript_path": "", "cwd": cwd})
    assert r.returncode == 0, f"STOP crashed on corrupt baseline: {r.stderr}"
    assert not baseline_path.exists(), "STOP must still clean up"


def test_pre_skips_phantom_entry_when_no_api_call(tmp_path):
    """A second PRE with an unchanged transcript (parallel tool calls) logs
    nothing; the next real API turn is logged once, deduped across its
    content-block lines, with cache creation counted."""
    import budgeter.lib.logger as lg

    project_dir = make_test_project(tmp_path / "project_phantom")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    session_id = make_session_id()
    transcript = tmp_path / "phantom.jsonl"
    first = {"input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 50}
    transcript.write_text("\n".join(_assistant_lines("m1", first)) + "\n", encoding="utf-8")
    payload = {
        "tool_name": "Bash",
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": cwd,
    }

    _with_flag_enabled("budgeter-log", project_dir)
    try:
        r = run_hook("pre_tool_use.py", payload)
        assert r.returncode == 0, r.stderr
        assert log_entry_count(log_path) == 0

        # Parallel tool call: same transcript, no API call in between.
        r = run_hook("pre_tool_use.py", payload)
        assert r.returncode == 0, r.stderr
        assert log_entry_count(log_path) == 0, "no API call -> no entry"

        second = {
            "input_tokens": 10,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 300,
            "output_tokens": 40,
        }
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(
                "\n".join(_assistant_lines("m2", second, ("thinking", "text", "tool_use"))) + "\n"
            )
        r = run_hook("pre_tool_use.py", payload)
        assert r.returncode == 0, r.stderr
        assert log_entry_count(log_path) == 1

        lg.configure_for_project(cwd)
        entry = [e for e in lg.read_log() if e.get("session_id") == session_id][0]
        assert entry["tool_name"] == "Bash"
        assert entry["tokens_delta"] == 1250, "three lines of one call must count once"
        assert entry["cache_creation_tokens_delta"] == 300
        # prompt grew from 1000 to 1210 (+210), plus 40 output.
        assert entry["net_tokens_delta"] == 250
    finally:
        lg.configure_for_project(cwd)
        lg.cleanup_session(session_id)


def test_old_schema_baseline_is_not_compared(tmp_path):
    """A baseline from the per-line counting era is 1.7-2.6x too large; the
    first PRE after upgrade must neither log a phantom '[compaction]' marker
    nor a cost entry against it, and must rewrite it with the new schema."""
    import budgeter.lib.logger as lg

    project_dir = make_test_project(tmp_path / "project_oldschema")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    tmp_dir = project_dir / ".claude" / "budgeter-tmp"
    session_id = make_session_id()
    transcript = tmp_path / "old.jsonl"
    usage = {"input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 50}
    transcript.write_text(
        "\n".join(_assistant_lines("m1", usage, ("text", "tool_use"))) + "\n", encoding="utf-8"
    )
    tmp_dir.mkdir(parents=True, exist_ok=True)
    old = {
        "tokens": 2100,
        "context_tokens": 1050,
        "baseline_input": 1000,
        "baseline_cache": 0,
        "baseline_output": 50,
        "prev_tool_name": "Bash",
        "prev_assistant_message": "",
        "turn_number": 1,
        "task_turn": 1,
        "user_message": "",
        "scope_flags": [],
        "predicted_cost": 0,
        "warning_fired": False,
        "agent_description": "",
    }
    (tmp_dir / f"{session_id}_baseline.json").write_text(json.dumps(old), encoding="utf-8")
    payload = {
        "tool_name": "Bash",
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": cwd,
    }
    _with_flag_enabled("budgeter-log", project_dir)
    r = run_hook("pre_tool_use.py", payload)
    assert r.returncode == 0, r.stderr
    assert log_entry_count(log_path) == 0, "no marker, no entry against an old-schema baseline"
    new = json.loads((tmp_dir / f"{session_id}_baseline.json").read_text(encoding="utf-8"))
    assert new["schema"] == lg.BASELINE_SCHEMA and new["tokens"] == 1050
    assert new["task_turn"] == 1, "turn continuity is kept"
    # STOP against a shrunk total logs nothing (compaction is the PRE's job).
    (tmp_dir / f"{session_id}_baseline.json").write_text(
        json.dumps({**new, "tokens": 5000}), encoding="utf-8"
    )
    r = run_hook(
        "stop_session.py",
        {"session_id": session_id, "transcript_path": str(transcript), "cwd": cwd},
    )
    assert r.returncode == 0, r.stderr
    assert log_entry_count(log_path) == 0


def test_post_agent_payload_over_64kb_is_logged(tmp_path):
    import budgeter.lib.logger as lg

    project_dir = make_test_project(tmp_path / "project_bigpost")
    cwd = str(project_dir)
    log_path = project_dir / ".claude" / "budgeter-log.jsonl"
    session_id = make_session_id()
    payload = {
        "tool_name": "Agent",
        "session_id": session_id,
        "cwd": cwd,
        "tool_input": {"description": "lens attacker", "prompt": "p" * 4000},
        "tool_response": {"totalTokens": 777, "content": "x" * 200_000},
    }
    assert len(json.dumps(payload)) > 64 * 1024
    _with_flag_enabled("budgeter-log", project_dir)
    r = run_hook("post_tool_use.py", payload)
    assert r.returncode == 0, r.stderr
    assert log_entry_count(log_path) == 1, f"large Agent payload must be logged: {r.stderr}"
    lg.configure_for_project(cwd)
    entry = [e for e in lg.read_log() if e.get("session_id") == session_id][0]
    assert entry["tokens_delta"] == 777 and entry["agent_type"] == "lens attacker"


def test_weighted_delta_counts_cache_creation(tmp_path):
    from budgeter import report

    w_input, w_cache, w_create, w_output = report._get_price_weights()
    e = {
        "input_tokens_delta": 100,
        "cache_tokens_delta": 1000,
        "cache_creation_tokens_delta": 200,
        "output_tokens_delta": 10,
    }
    assert report.weighted_delta(e) == int(
        100 * w_input + 1000 * w_cache + 200 * w_create + 10 * w_output
    )
    assert w_create > w_input, "cache writes bill above plain input"


# ---------------------------------------------------------------------------
# Review 2026-08 Phase 2: the warning subsystem is gone, its data is not
# ---------------------------------------------------------------------------


def test_report_reads_entries_written_before_the_warning_feature(tmp_path):
    """26k logged entries carry ``scope_flags`` and the feedback-era shape.
    Deleting the field must not make a single one of them unreadable."""
    import io
    from contextlib import redirect_stdout

    from budgeter import report

    old = {
        "timestamp": "2026-04-02T10:00:00+00:00",
        "session_id": "s1",
        "tool_name": "Bash",
        "assistant_message": "I'll refactor auth.py",
        "user_message": "why is this slow",
        "tokens_delta": 9000,
        "context_tokens": 4000,
        "net_tokens_delta": 5000,
        "turn_number": 2,
        "task_turn": 1,
        "scope_flags": ["scope_keywords", "investigative_keywords"],
        "predicted_cost": 80000,
        "warning_fired": True,
        "project": "",
    }
    log = tmp_path / "old_log.jsonl"
    log.write_text(json.dumps(old) + "\n", encoding="utf-8")

    orig_log = report.LOG_PATH
    report.LOG_PATH = log
    buf = io.StringIO()
    try:
        entries = report.load_entries()
        assert len(entries) == 1, "an old entry must still parse"
        assert report.net_delta(entries[0]) == 5000
        with redirect_stdout(buf):
            report.print_by_turn(entries)
            report.print_flat(entries)
            report.print_grouped(entries)
    finally:
        report.LOG_PATH = orig_log
    assert "5,000" in buf.getvalue()


def test_estimator_exposes_only_the_session_nudge(tmp_path):
    """The warning rules, scoring and magnitude estimate are deleted, not
    merely unreferenced."""
    from budgeter.lib import estimator

    gone = [
        "detect_scope_flags",
        "estimate_magnitude",
        "score_flags",
        "is_approval_message",
        "_group_tasks",
    ]
    for name in gone:
        assert not hasattr(estimator, name), f"{name} should be deleted"
    assert hasattr(estimator, "session_length_nudge")

    import budgeter.lib.logger as lg

    for name in [
        "append_feedback",
        "append_feedback_if_not_present",
        "read_feedback",
        "count_entries",
        "save_snapshot",
        "load_snapshot",
        "delete_snapshot",
        "FEEDBACK_PATH",
    ]:
        assert not hasattr(lg, name), f"logger.{name} should be deleted"


if __name__ == "__main__":
    main()
