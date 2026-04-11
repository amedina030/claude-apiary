#!/usr/bin/env python3
"""Tests for runner/run.py orchestrator."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

from runner import run as orchestrator
from runner.run import run_stage, main, STAGES, SCRIPT_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_completed_process(returncode=0, stdout="ok", stderr=""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def make_popen(returncode=0, stdout="ok", stderr="", timeout_after_calls=0):
    """Build a Popen-shaped mock for run_stage's new Popen+communicate path.

    `timeout_after_calls`: if >0, communicate raises TimeoutExpired the first
    time and returns normally on the second call (the kill-and-drain path).
    """
    proc = MagicMock()
    proc.returncode = returncode
    state = {"calls": 0}
    def _comm(timeout=None):
        state["calls"] += 1
        if timeout_after_calls and state["calls"] <= timeout_after_calls:
            raise subprocess.TimeoutExpired(cmd="test", timeout=timeout or 0)
        return (stdout, stderr)
    proc.communicate.side_effect = _comm
    proc.poll.return_value = returncode
    return proc


class _RunnerTestCase(unittest.TestCase):
    """Base TestCase that supplies a tmp dir, intake fixtures, and stdout/stderr capture."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def make_intake_data(self):
        return {
            "id": str(uuid.uuid4()),
            "title": "Test Feature Implementation",
            "problem": "This is a test problem description that is at least twenty characters long",
            "description": "This is a test description that is at least twenty characters long",
            "scope": ["runner/run.py"],
            "created_at": "2026-04-05T12:00:00Z",
        }

    def make_intake_file(self, intake_data=None):
        if intake_data is None:
            intake_data = self.make_intake_data()
        f = self.tmp_path / "intake" / f"{intake_data['id']}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(intake_data), encoding="utf-8")
        return f, intake_data

    def _run_main_capture(self, argv):
        """Run orchestrator.main() with patched argv and captured stdout/stderr.

        Returns a tuple of ``(exit_code, stdout, stderr)``. ``exit_code`` is
        ``None`` if main() returned normally without calling sys.exit().
        """
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        exit_code = None
        with patch.object(sys, "argv", argv), \
             patch.object(sys, "stdout", out_buf), \
             patch.object(sys, "stderr", err_buf):
            try:
                orchestrator.main()
            except SystemExit as exc:
                exit_code = exc.code
        return exit_code, out_buf.getvalue(), err_buf.getvalue()


# ---------------------------------------------------------------------------
# run_stage() unit tests
# ---------------------------------------------------------------------------


class TestRunStage(_RunnerTestCase):

    def test_module_not_found(self):
        input_f = self.tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        ok, _, msg, elapsed = orchestrator.run_stage("test", "nonexistent", input_f)
        self.assertFalse(ok)
        self.assertIn("Stage module not found: runner.nonexistent", msg)
        self.assertEqual(elapsed, 0.0)

    def test_input_file_not_found(self):
        input_f = self.tmp_path / "nonexistent.json"
        # Use a real module name so the module-exists check passes
        ok, _, msg, elapsed = orchestrator.run_stage("test", "validate_intake", input_f)
        self.assertFalse(ok)
        self.assertIn(f"Stage input file not found: {input_f}", msg)
        self.assertEqual(elapsed, 0.0)

    @patch("runner.run.subprocess.Popen")
    def test_success(self, mock_popen):
        input_f = self.tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        mock_popen.return_value = make_popen(returncode=0, stdout="output here")
        ok, output, _, elapsed = orchestrator.run_stage("test", "validate_intake", input_f)
        self.assertTrue(ok)
        self.assertEqual(output, "output here")
        self.assertGreaterEqual(elapsed, 0)
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[1], "-m")
        self.assertEqual(args[2], "runner.validate_intake")
        self.assertEqual(args[3], str(input_f))

    @patch("runner.run.subprocess.Popen")
    def test_failure_returncode(self, mock_popen):
        input_f = self.tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        mock_popen.return_value = make_popen(returncode=1, stderr="some error")
        ok, _, output, elapsed = orchestrator.run_stage("test", "validate_intake", input_f)
        self.assertFalse(ok)
        self.assertEqual(output, "some error")
        self.assertGreaterEqual(elapsed, 0)

    @patch("runner.run.subprocess.Popen")
    def test_timeout(self, mock_popen):
        input_f = self.tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        # First communicate() raises TimeoutExpired, second (after kill) returns.
        mock_popen.return_value = make_popen(timeout_after_calls=1)
        ok, _, output, _elapsed = orchestrator.run_stage("test", "validate_intake", input_f)
        self.assertFalse(ok)
        self.assertIn("Stage timed out", output)

    @patch("runner.run.subprocess.Popen")
    def test_oserror(self, mock_popen):
        input_f = self.tmp_path / "input.json"
        input_f.write_text("{}", encoding="utf-8")
        mock_popen.side_effect = OSError("spawn failed")
        ok, _, output, _elapsed = orchestrator.run_stage("test", "validate_intake", input_f)
        self.assertFalse(ok)
        self.assertIn("Stage failed to launch: spawn failed", output)


# ---------------------------------------------------------------------------
# main() CLI validation tests
# ---------------------------------------------------------------------------


class TestMainCLIValidation(_RunnerTestCase):

    def _write_intake_with(self, name, payload):
        f = self.tmp_path / name
        f.write_text(json.dumps(payload), encoding="utf-8")
        return f

    def test_no_args(self):
        code, _, err = self._run_main_capture(["run.py"])
        self.assertEqual(code, 1)
        self.assertIn("intake path is required", err)

    def test_nonexistent_file(self):
        fake = self.tmp_path / "nope.json"
        code, _, err = self._run_main_capture(["run.py", str(fake)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file not found", err)

    def test_invalid_json(self):
        bad = self.tmp_path / "bad.json"
        bad.write_text("not json!", encoding="utf-8")
        code, _, err = self._run_main_capture(["run.py", str(bad)])
        self.assertEqual(code, 1)
        self.assertIn("Invalid intake JSON", err)

    def test_missing_id_field(self):
        f = self._write_intake_with("no_id.json", {"title": "x"})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file missing id field", err)

    def test_empty_id(self):
        f = self._write_intake_with("empty_id.json", {"id": ""})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file missing id field", err)

    def test_whitespace_only_id(self):
        f = self._write_intake_with("ws_id.json", {"id": "   "})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file missing id field", err)

    def test_integer_id(self):
        f = self._write_intake_with("int_id.json", {"id": 42})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file missing id field", err)

    def test_none_id(self):
        f = self._write_intake_with("none_id.json", {"id": None})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file missing id field", err)

    def test_list_id(self):
        f = self._write_intake_with("list_id.json", {"id": ["a", "b"]})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file missing id field", err)

    def test_bool_id(self):
        f = self._write_intake_with("bool_id.json", {"id": True})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("Intake file missing id field", err)

    def test_path_traversal_forward_slash(self):
        f = self._write_intake_with("trav.json", {"id": "../traversal"})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("invalid characters", err)

    def test_path_traversal_backslash(self):
        f = self._write_intake_with("bs.json", {"id": "a\\b"})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("invalid characters", err)

    def test_path_traversal_simple_slash(self):
        f = self._write_intake_with("slash.json", {"id": "a/b"})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("invalid characters", err)

    def test_dot_id(self):
        """POSIX Path('.').name == '.' so the Path comparison alone would
        pass; requires an explicit dot check."""
        f = self._write_intake_with("dot_id.json", {"id": "."})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("invalid characters", err)

    def test_dotdot_id(self):
        """Same POSIX Path edge case as dot."""
        f = self._write_intake_with("dotdot_id.json", {"id": ".."})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("invalid characters", err)

    def test_null_byte_id(self):
        """Null bytes can truncate filenames on some filesystems and are a
        path-injection vector."""
        f = self._write_intake_with("null_id.json", {"id": "foo\x00bar"})
        code, _, err = self._run_main_capture(["run.py", str(f)])
        self.assertEqual(code, 1)
        self.assertIn("invalid characters", err)


# ---------------------------------------------------------------------------
# main() happy path and stage ordering tests
# ---------------------------------------------------------------------------


class TestMainHappyPath(_RunnerTestCase):

    @patch("runner.run.run_stage")
    def test_all_stages_succeed(self, mock_run_stage):
        intake_file, _ = self.make_intake_file()
        mock_run_stage.return_value = (True, "stage output", "", 1.0)
        code, out, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertIn(code, (None, 0))
        self.assertIn("COMPLETE", out)
        self.assertIn("Stages completed: 6/6", out)
        self.assertEqual(mock_run_stage.call_count, 6)

    @patch("runner.run.run_stage")
    def test_stage_call_order(self, mock_run_stage):
        intake_file, _ = self.make_intake_file()
        mock_run_stage.return_value = (True, "ok", "", 0.5)
        code, _, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertIn(code, (None, 0))
        expected_modules = [
            "validate_intake",
            "auto_refine",
            "auto_plan",
            "executor",
            "auto_harden",
            "approval",
        ]
        self.assertEqual(mock_run_stage.call_count, 6)
        for i, c in enumerate(mock_run_stage.call_args_list):
            name_arg = c[0][0]
            module_arg = c[0][1]
            self.assertEqual(module_arg, expected_modules[i])
            self.assertEqual(name_arg, orchestrator.STAGES[i][0])

    @patch("runner.run.run_stage")
    def test_artifact_path_wiring(self, mock_run_stage):
        intake_file, intake_data = self.make_intake_file()
        uid = intake_data["id"]
        mock_run_stage.return_value = (True, "ok", "", 0.5)
        # Verify the override condition is exercised: intake_file must not be
        # in SCRIPT_DIR/intake/
        default_intake = orchestrator.SCRIPT_DIR / "intake" / f"{uid}.json"
        self.assertNotEqual(
            intake_file.resolve(), default_intake.resolve(),
            "Test setup error: intake_file must not be in runner/intake/ or the override path is never taken",
        )
        code, _, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertIn(code, (None, 0))
        calls = mock_run_stage.call_args_list
        # Stage 1 (validate_intake) and 2 (auto_refine): intake path
        self.assertEqual(calls[0].args[2], intake_file.resolve())
        self.assertEqual(calls[1].args[2], intake_file.resolve())
        # Stage 3: specs/{uuid}.json
        self.assertEqual(calls[2].args[2], orchestrator.SCRIPT_DIR / "specs" / f"{uid}.json")
        # Stage 4: plans/{uuid}.json
        self.assertEqual(calls[3].args[2], orchestrator.SCRIPT_DIR / "plans" / f"{uid}.json")
        # Stage 5: executions/{uuid}.json
        self.assertEqual(calls[4].args[2], orchestrator.SCRIPT_DIR / "executions" / f"{uid}.json")
        # Stage 6: hardens/{uuid}.json
        self.assertEqual(calls[5].args[2], orchestrator.SCRIPT_DIR / "hardens" / f"{uid}.json")

    @patch("runner.run.run_stage")
    def test_empty_stdout_no_arrow_line(self, mock_run_stage):
        intake_file, _ = self.make_intake_file()
        mock_run_stage.return_value = (True, "", "", 0.5)
        code, out, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertIn(code, (None, 0))
        self.assertIn("PASSED", out)
        self.assertNotIn("  -> ", out)

    @patch("runner.run.run_stage")
    def test_final_stage_empty_stdout_verdict_unknown(self, mock_run_stage):
        """When the final stage (approval) returns empty stdout, verdict_line
        is 'unknown'. Covers run.py:157 where empty final_output triggers the
        'unknown' fallback."""
        intake_file, _ = self.make_intake_file()

        def side_effect(name, script, input_path):
            if name == "approval":
                return (True, "", "", 1.0)
            return (True, "ok", "", 0.5)

        mock_run_stage.side_effect = side_effect
        code, out, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertIn(code, (None, 0))
        self.assertIn("COMPLETE: unknown", out)


# ---------------------------------------------------------------------------
# main() failure propagation tests
# ---------------------------------------------------------------------------


class TestMainFailurePropagation(_RunnerTestCase):

    @patch("runner.run.run_stage")
    def test_stage3_failure_stops_runner(self, mock_run_stage):
        intake_file, _ = self.make_intake_file()

        def side_effect(name, script, input_path):
            if name == "auto_plan":
                return (False, "", "plan error", 2.0)
            return (True, "ok", "", 0.5)

        mock_run_stage.side_effect = side_effect
        code, out, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertEqual(code, 1)
        self.assertIn("FAILED at stage 3: auto_plan", out)
        self.assertIn("Stages completed: 2/6", out)
        self.assertEqual(mock_run_stage.call_count, 3)

    @patch("runner.run.run_stage")
    def test_stage1_failure_stops_all(self, mock_run_stage):
        intake_file, _ = self.make_intake_file()
        mock_run_stage.return_value = (False, "", "validate error", 0.1)
        code, out, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertEqual(code, 1)
        self.assertEqual(mock_run_stage.call_count, 1)
        self.assertIn("FAILED at stage 1: validate_intake", out)
        self.assertIn("Stages completed: 0/6", out)

    @patch("runner.run.run_stage")
    def test_failure_shows_stderr_lines(self, mock_run_stage):
        intake_file, _ = self.make_intake_file()
        mock_run_stage.return_value = (False, "", "line1\nline2\nline3", 1.0)
        code, out, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertEqual(code, 1)
        self.assertIn("  ! line1", out)
        self.assertIn("  ! line2", out)
        self.assertIn("  ! line3", out)


# ---------------------------------------------------------------------------
# KeyboardInterrupt test
# ---------------------------------------------------------------------------


class TestMainKeyboardInterrupt(_RunnerTestCase):

    @patch("runner.run.run_stage")
    def test_interrupt_during_stage4(self, mock_run_stage):
        intake_file, _ = self.make_intake_file()

        def side_effect(name, script, input_path):
            if name == "executor":  # stage 4
                raise KeyboardInterrupt()
            return (True, "ok", "", 0.5)

        mock_run_stage.side_effect = side_effect
        code, out, _ = self._run_main_capture(["run.py", str(intake_file)])
        self.assertEqual(code, 1)
        self.assertIn("Interrupted during stage 4: executor", out)
        self.assertIn("Stages completed: 3/6", out)


# ---------------------------------------------------------------------------
# Intake path override edge case
# ---------------------------------------------------------------------------


class TestIntakePathOverride(_RunnerTestCase):

    @patch("runner.run.run_stage")
    def test_intake_outside_runner_dir(self, mock_run_stage):
        intake_data = self.make_intake_data()
        f = self.tmp_path / "elsewhere" / "myfile.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(intake_data), encoding="utf-8")
        mock_run_stage.return_value = (True, "ok", "", 0.5)
        self._run_main_capture(["run.py", str(f)])
        calls = mock_run_stage.call_args_list
        self.assertEqual(calls[0].args[2], f.resolve())
        self.assertEqual(calls[1].args[2], f.resolve())


if __name__ == "__main__":
    unittest.main()
