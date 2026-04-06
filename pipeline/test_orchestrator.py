#!/usr/bin/env python3
"""Tests for pipeline/run.py orchestrator."""
import json
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# Import the module under test
from pipeline import run as orchestrator
# Also: from pipeline.run import run_stage, main, STAGES, SCRIPT_DIR
from pipeline.run import run_stage, main, STAGES, SCRIPT_DIR


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def intake_data():
    """Return a valid intake dict with all required fields."""
    return {
        "id": str(uuid.uuid4()),
        "title": "Test Feature Implementation",
        "problem": "This is a test problem description that is at least twenty characters long",
        "description": "This is a test description that is at least twenty characters long",
        "scope": ["pipeline/run.py"],
        "created_at": "2026-04-05T12:00:00Z",
    }


@pytest.fixture
def intake_file(tmp_path, intake_data):
    """Write intake_data to a temp JSON file and return its Path."""
    f = tmp_path / "intake" / f"{intake_data['id']}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(intake_data), encoding="utf-8")
    return f



# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_completed_process(returncode=0, stdout="ok", stderr=""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# run_stage() unit tests
# ---------------------------------------------------------------------------

class TestRunStage:

    def test_script_not_found(self, tmp_path):
        """run_stage returns failure when script_path doesn't exist."""
        script = tmp_path / "nonexistent.py"
        input_f = tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        ok, _, msg, elapsed = orchestrator.run_stage("test", script, input_f)
        assert ok is False
        assert f"Stage script not found: {script}" in msg
        assert elapsed == 0.0

    def test_input_file_not_found(self, tmp_path):
        """run_stage returns failure when input_path doesn't exist."""
        script = tmp_path / "script.py"
        script.write_text("pass", encoding="utf-8")
        input_f = tmp_path / "nonexistent.json"
        ok, _, msg, elapsed = orchestrator.run_stage("test", script, input_f)
        assert ok is False
        assert f"Stage input file not found: {input_f}" in msg
        assert elapsed == 0.0

    @patch("pipeline.run.subprocess.run")
    def test_success(self, mock_run, tmp_path):
        """run_stage returns (True, stdout, elapsed) on returncode=0."""
        script = tmp_path / "script.py"
        script.write_text("pass", encoding="utf-8")
        input_f = tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        mock_run.return_value = make_completed_process(returncode=0, stdout="output here")
        ok, output, _, elapsed = orchestrator.run_stage("test", script, input_f)
        assert ok is True
        assert output == "output here"
        assert elapsed > 0 or elapsed == pytest.approx(0, abs=1)  # time-based
        mock_run.assert_called_once()
        # Verify call args include sys.executable, script path, input path
        args = mock_run.call_args[0][0]
        assert args[0] == sys.executable
        assert args[1] == str(script)
        assert args[2] == str(input_f)

    @patch("pipeline.run.subprocess.run")
    def test_failure_returncode(self, mock_run, tmp_path):
        """run_stage returns (False, stderr, elapsed) on non-zero returncode."""
        script = tmp_path / "script.py"
        script.write_text("pass", encoding="utf-8")
        input_f = tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        mock_run.return_value = make_completed_process(returncode=1, stderr="some error")
        ok, _, output, elapsed = orchestrator.run_stage("test", script, input_f)
        assert ok is False
        assert output == "some error"
        assert elapsed > 0 or elapsed == pytest.approx(0, abs=1)

    @patch("pipeline.run.subprocess.run")
    def test_timeout(self, mock_run, tmp_path):
        """run_stage returns failure with timeout message on TimeoutExpired."""
        script = tmp_path / "script.py"
        script.write_text("pass", encoding="utf-8")
        input_f = tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=3600)
        ok, _, output, elapsed = orchestrator.run_stage("test", script, input_f)
        assert ok is False
        assert "Stage timed out" in output

    @patch("pipeline.run.subprocess.run")
    def test_oserror(self, mock_run, tmp_path):
        """run_stage returns failure with launch error on OSError."""
        script = tmp_path / "script.py"
        script.write_text("pass", encoding="utf-8")
        input_f = tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        mock_run.side_effect = OSError("spawn failed")
        ok, _, output, elapsed = orchestrator.run_stage("test", script, input_f)
        assert ok is False
        assert "Stage failed to launch: spawn failed" in output


# ---------------------------------------------------------------------------
# main() CLI validation tests
# ---------------------------------------------------------------------------

class TestMainCLIValidation:

    def test_no_args(self, monkeypatch, capsys):
        """main() exits 1 with usage message when no args provided."""
        monkeypatch.setattr(sys, "argv", ["run.py"])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Usage:" in capsys.readouterr().err

    def test_nonexistent_file(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when intake file doesn't exist."""
        fake = tmp_path / "nope.json"
        monkeypatch.setattr(sys, "argv", ["run.py", str(fake)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file not found" in capsys.readouterr().err

    def test_invalid_json(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when intake file contains bad JSON."""
        bad = tmp_path / "bad.json"
        bad.write_text("not json!", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(bad)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Invalid intake JSON" in capsys.readouterr().err

    def test_missing_id_field(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when JSON has no 'id' field."""
        f = tmp_path / "no_id.json"
        f.write_text(json.dumps({"title": "x"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file missing id field" in capsys.readouterr().err

    def test_empty_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is empty string."""
        f = tmp_path / "empty_id.json"
        f.write_text(json.dumps({"id": ""}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file missing id field" in capsys.readouterr().err

    def test_whitespace_only_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is whitespace-only."""
        f = tmp_path / "ws_id.json"
        f.write_text(json.dumps({"id": "   "}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file missing id field" in capsys.readouterr().err

    def test_integer_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is an integer (non-string type)."""
        f = tmp_path / "int_id.json"
        f.write_text(json.dumps({"id": 42}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file missing id field" in capsys.readouterr().err

    def test_none_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is None (non-string type)."""
        f = tmp_path / "none_id.json"
        f.write_text(json.dumps({"id": None}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file missing id field" in capsys.readouterr().err

    def test_list_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is a list (non-string type)."""
        f = tmp_path / "list_id.json"
        f.write_text(json.dumps({"id": ["a", "b"]}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file missing id field" in capsys.readouterr().err

    def test_bool_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is a bool (non-string type)."""
        f = tmp_path / "bool_id.json"
        f.write_text(json.dumps({"id": True}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "Intake file missing id field" in capsys.readouterr().err

    def test_path_traversal_forward_slash(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' contains '../traversal'."""
        f = tmp_path / "trav.json"
        f.write_text(json.dumps({"id": "../traversal"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "invalid characters" in capsys.readouterr().err

    def test_path_traversal_backslash(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' contains backslash."""
        f = tmp_path / "bs.json"
        f.write_text(json.dumps({"id": "a\\b"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "invalid characters" in capsys.readouterr().err

    def test_path_traversal_simple_slash(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' contains a forward slash (no '..' needed)."""
        f = tmp_path / "slash.json"
        f.write_text(json.dumps({"id": "a/b"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "invalid characters" in capsys.readouterr().err

    def test_dot_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is '.' — POSIX Path('.').name == '.' so the
        Path comparison alone would pass; requires an explicit dot check."""
        f = tmp_path / "dot_id.json"
        f.write_text(json.dumps({"id": "."}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "invalid characters" in capsys.readouterr().err

    def test_dotdot_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' is '..' — same POSIX Path edge case as dot."""
        f = tmp_path / "dotdot_id.json"
        f.write_text(json.dumps({"id": ".."}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "invalid characters" in capsys.readouterr().err

    def test_null_byte_id(self, monkeypatch, capsys, tmp_path):
        """main() exits 1 when 'id' contains a null byte — null bytes can truncate
        filenames on some filesystems and are a path-injection vector."""
        f = tmp_path / "null_id.json"
        f.write_text(json.dumps({"id": "foo\x00bar"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert "invalid characters" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() happy path and stage ordering tests
# ---------------------------------------------------------------------------

class TestMainHappyPath:

    @patch("pipeline.run.run_stage")
    def test_all_stages_succeed(self, mock_run_stage, monkeypatch, capsys, intake_file, intake_data):
        """All 6 stages succeed: exits 0, prints COMPLETE, Stages completed: 6/6."""
        mock_run_stage.return_value = (True, "stage output", "", 1.0)
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        # main() currently returns normally on success; handle both return and sys.exit(0)
        try:
            orchestrator.main()
        except SystemExit as exc:
            assert exc.code == 0, f"main() exited with non-zero code: {exc.code}"
        out = capsys.readouterr().out
        assert "COMPLETE" in out
        assert "Stages completed: 6/6" in out
        assert mock_run_stage.call_count == 6

    @patch("pipeline.run.run_stage")
    def test_stage_call_order(self, mock_run_stage, monkeypatch, capsys, intake_file, intake_data):
        """Stages are called in STAGES order with correct script names."""
        mock_run_stage.return_value = (True, "ok", "", 0.5)
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        try:
            orchestrator.main()
        except SystemExit as exc:
            assert exc.code == 0, f"Unexpected exit code: {exc.code}"
        expected_scripts = [
            "validate_intake.py",
            "auto_refine.py",
            "auto_plan.py",
            "executor.py",
            "auto_harden.py",
            "approval.py",
        ]
        assert mock_run_stage.call_count == 6
        for i, c in enumerate(mock_run_stage.call_args_list):
            # call args: (name, script_path, input_path)
            name_arg = c[0][0]  # positional arg 0
            script_arg = c[0][1]  # positional arg 1 (Path)
            assert script_arg.name == expected_scripts[i]
            assert name_arg == orchestrator.STAGES[i][0]

    @patch("pipeline.run.run_stage")
    def test_artifact_path_wiring(self, mock_run_stage, monkeypatch, capsys, intake_file, intake_data):
        """Each stage receives the correct input artifact path."""
        uid = intake_data["id"]
        mock_run_stage.return_value = (True, "ok", "", 0.5)
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        # Verify the override condition is exercised: intake_file must not be in SCRIPT_DIR/intake/
        default_intake = orchestrator.SCRIPT_DIR / "intake" / f"{uid}.json"
        assert intake_file.resolve() != default_intake.resolve(), \
            "Test setup error: intake_file must not be in pipeline/intake/ or the override path is never taken"
        try:
            orchestrator.main()
        except SystemExit as exc:
            assert exc.code == 0, f"Unexpected exit code: {exc.code}"
        calls = mock_run_stage.call_args_list
        # Stage 1 (validate_intake) and 2 (auto_refine): intake path
        # Since intake_file is in tmp_path (not SCRIPT_DIR/intake/), the override applies
        assert calls[0].args[2] == intake_file.resolve()  # stage 1 input = intake
        assert calls[1].args[2] == intake_file.resolve()  # stage 2 input = intake
        # Stage 3: specs/{uuid}.json
        assert calls[2].args[2] == orchestrator.SCRIPT_DIR / "specs" / f"{uid}.json"
        # Stage 4: plans/{uuid}.json
        assert calls[3].args[2] == orchestrator.SCRIPT_DIR / "plans" / f"{uid}.json"
        # Stage 5: executions/{uuid}.json
        assert calls[4].args[2] == orchestrator.SCRIPT_DIR / "executions" / f"{uid}.json"
        # Stage 6: hardens/{uuid}.json
        assert calls[5].args[2] == orchestrator.SCRIPT_DIR / "hardens" / f"{uid}.json"

    @patch("pipeline.run.run_stage")
    def test_empty_stdout_no_arrow_line(self, mock_run_stage, monkeypatch, capsys, intake_file):
        """When a successful stage returns empty stdout, no '-> ...' line is printed."""
        mock_run_stage.return_value = (True, "", "", 0.5)
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        try:
            orchestrator.main()
        except SystemExit as exc:
            assert exc.code == 0, f"Unexpected exit code: {exc.code}"
        out = capsys.readouterr().out
        assert "PASSED" in out
        assert "  -> " not in out

    @patch("pipeline.run.run_stage")
    def test_final_stage_empty_stdout_verdict_unknown(self, mock_run_stage, monkeypatch, capsys, intake_file):
        """When the final stage (approval) returns empty stdout, verdict_line is 'unknown'.
        Covers run.py:157 where empty final_output triggers the 'unknown' fallback."""
        def side_effect(name, script, input_path):
            if name == "approval":
                return (True, "", "", 1.0)
            return (True, "ok", "", 0.5)
        mock_run_stage.side_effect = side_effect
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        try:
            orchestrator.main()
        except SystemExit as exc:
            assert exc.code == 0, f"Unexpected exit code: {exc.code}"
        out = capsys.readouterr().out
        assert "COMPLETE: unknown" in out


# ---------------------------------------------------------------------------
# main() failure propagation tests
# ---------------------------------------------------------------------------

class TestMainFailurePropagation:

    @patch("pipeline.run.run_stage")
    def test_stage3_failure_stops_pipeline(self, mock_run_stage, monkeypatch, capsys, intake_file):
        """When stage 3 fails, stages 4-6 don't run. Output shows FAILED at stage 3."""
        def side_effect(name, script, input_path):
            if name == "auto_plan":
                return (False, "", "plan error", 2.0)
            return (True, "ok", "", 0.5)
        mock_run_stage.side_effect = side_effect
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "FAILED at stage 3: auto_plan" in out
        assert "Stages completed: 2/6" in out
        # Only 3 calls: stages 1, 2, and 3 (which failed)
        assert mock_run_stage.call_count == 3

    @patch("pipeline.run.run_stage")
    def test_stage1_failure_stops_all(self, mock_run_stage, monkeypatch, capsys, intake_file):
        """When stage 1 fails, no subsequent stages run."""
        mock_run_stage.return_value = (False, "", "validate error", 0.1)
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        assert mock_run_stage.call_count == 1
        out = capsys.readouterr().out
        assert "FAILED at stage 1: validate_intake" in out
        assert "Stages completed: 0/6" in out

    @patch("pipeline.run.run_stage")
    def test_failure_shows_stderr_lines(self, mock_run_stage, monkeypatch, capsys, intake_file):
        """Failed stage stderr lines (up to 5) are prefixed with '! '."""
        mock_run_stage.return_value = (False, "", "line1\nline2\nline3", 1.0)
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        with pytest.raises(SystemExit):
            orchestrator.main()
        out = capsys.readouterr().out
        assert "  ! line1" in out
        assert "  ! line2" in out
        assert "  ! line3" in out


# ---------------------------------------------------------------------------
# KeyboardInterrupt test
# ---------------------------------------------------------------------------

class TestMainKeyboardInterrupt:

    @patch("pipeline.run.run_stage")
    def test_interrupt_during_stage4(self, mock_run_stage, monkeypatch, capsys, intake_file):
        """KeyboardInterrupt during stage 4 prints interrupted message and exits 1."""
        def side_effect(name, script, input_path):
            if name == "executor":  # stage 4
                raise KeyboardInterrupt()
            return (True, "ok", "", 0.5)

        mock_run_stage.side_effect = side_effect
        monkeypatch.setattr(sys, "argv", ["run.py", str(intake_file)])
        with pytest.raises(SystemExit) as exc:
            orchestrator.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Interrupted during stage 4: executor" in out
        assert "Stages completed: 3/6" in out


# ---------------------------------------------------------------------------
# Intake path override edge case
# ---------------------------------------------------------------------------

class TestIntakePathOverride:

    @patch("pipeline.run.run_stage")
    def test_intake_outside_pipeline_dir(self, mock_run_stage, monkeypatch, capsys, tmp_path, intake_data):
        """When intake file is outside pipeline/intake/, artifacts['intake'] is overridden to the provided path."""
        # Write intake to a location that does NOT match SCRIPT_DIR/intake/{uuid}.json
        f = tmp_path / "elsewhere" / "myfile.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(intake_data), encoding="utf-8")
        mock_run_stage.return_value = (True, "ok", "", 0.5)
        monkeypatch.setattr(sys, "argv", ["run.py", str(f)])
        orchestrator.main()
        # Stage 1 and 2 should receive the resolved provided path
        calls = mock_run_stage.call_args_list
        assert calls[0].args[2] == f.resolve()
        assert calls[1].args[2] == f.resolve()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
