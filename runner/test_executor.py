#!/usr/bin/env python3
"""Tests for runner/executor.py helpers.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
Focused on pure helpers; full main() integration is exercised by live
runner runs.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import executor
from executor import (
    execute_step,
    load_previous_log,
    assert_files_clean,
    assert_no_unexpected_writes,
    _assert_action_matches_staged,
    validate_resume_state,
    persist_execution_log,
    build_step_prompt,
    build_verify_prompt,
    parse_verify_output,
)


class TestLoadPreviousLog(unittest.TestCase):
    """load_previous_log lets executor.main() preserve per-step status from
    prior runs on resume — especially verify/test steps, which don't commit
    and therefore can't be recovered from git log alone (item D of the
    runner-executor-architecture-hardening-from-t4-failures ticket)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_log(self, payload):
        log_path = self.tmp_path / "log.json"
        log_path.write_text(json.dumps(payload), encoding="utf-8")
        return log_path

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_previous_log(self.tmp_path / "nope.json"), {})

    def test_malformed_json_returns_empty(self):
        log_path = self.tmp_path / "log.json"
        log_path.write_text("not json {{{", encoding="utf-8")
        self.assertEqual(load_previous_log(log_path), {})

    def test_empty_steps_returns_empty(self):
        log_path = self._write_log({"uuid": "x", "steps": []})
        self.assertEqual(load_previous_log(log_path), {})

    def test_keyed_by_step_number(self):
        log_path = self._write_log({
            "uuid": "x",
            "steps": [
                {"step_number": 1, "status": "passed"},
                {"step_number": 2, "status": "failed", "error": "boom"},
            ],
        })
        result = load_previous_log(log_path)
        self.assertEqual(set(result.keys()), {1, 2})
        self.assertEqual(result[2]["error"], "boom")

    def test_skips_entries_without_int_step_number(self):
        log_path = self._write_log({
            "steps": [
                {"step_number": 1, "status": "passed"},
                {"step_number": "two", "status": "passed"},  # bad type
                "not-a-dict",  # not a dict
                {"status": "passed"},  # missing step_number
            ],
        })
        self.assertEqual(set(load_previous_log(log_path).keys()), {1})

    def test_preserves_full_entry(self):
        # Carried-forward entries should retain rich fields the bland
        # stub doesn't have (e.g. tool_uses, duration, custom keys)
        log_path = self._write_log({
            "steps": [
                {
                    "step_number": 5,
                    "status": "passed",
                    "files_changed": ["runner/x.py"],
                    "duration_ms": 12345,
                    "custom": "value",
                },
            ],
        })
        entry = load_previous_log(log_path)[5]
        self.assertEqual(entry["duration_ms"], 12345)
        self.assertEqual(entry["custom"], "value")
        self.assertEqual(entry["files_changed"], ["runner/x.py"])


class TestExecuteStepTestAction(unittest.TestCase):
    """execute_step('test', ...) must not retry on failure (test files
    don't change between attempts in the same run, so retries waste a slot)
    and must store the FULL test output (not a truncated prefix) so
    diagnosis isn't blocked by mid-traceback cutoffs. Both behaviors guard
    sub-bugs (2) and (3) from TODO #197."""

    def test_failed_test_action_does_not_retry(self):
        step = {"step_number": 1, "action": "test", "code_spec": "fail-me", "files": []}
        call_count = {"n": 0}

        def fake_run(_):
            call_count["n"] += 1
            return False, "boom"

        with mock.patch.object(executor, "run_test_command", side_effect=fake_run):
            result = execute_step(step, spec={}, model="sonnet")

        self.assertEqual(call_count["n"], 1, "test action must not retry")
        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["error"])

    def test_failed_test_action_stores_full_output(self):
        # Long traceback-shaped output — must be preserved verbatim, not
        # truncated at 500 chars (the previous behavior cut tracebacks
        # mid-line at 'File "D:\\Professional\\claude-' on T4).
        long_output = "Traceback line " + ("X" * 2000)
        step = {"step_number": 1, "action": "test", "code_spec": "x", "files": []}

        with mock.patch.object(executor, "run_test_command", return_value=(False, long_output)):
            result = execute_step(step, spec={}, model="sonnet")

        self.assertIn(long_output, result["error"])
        self.assertGreater(len(result["error"]), 1500)


class TestParseVerifyOutput(unittest.TestCase):
    """#252: the verify-output parser handles every envelope the runner
    has actually seen — bare JSON, Claude Code {result:...} envelope,
    markdown fences, JSON embedded in prose, and combinations. None of
    those branches had coverage before the extraction."""

    def test_bare_json_passed_true(self):
        out = parse_verify_output('{"passed": true, "explanation": "ok"}')
        self.assertTrue(out["passed"])
        self.assertEqual(out["explanation"], "ok")

    def test_bare_json_passed_false(self):
        out = parse_verify_output('{"passed": false, "explanation": "missing"}')
        self.assertFalse(out["passed"])
        self.assertEqual(out["explanation"], "missing")

    def test_envelope_with_result_inner_json(self):
        # Claude Code wraps stdout in {"result": "<text>", ...}.
        raw = json.dumps({"result": '{"passed": true, "explanation": "yes"}'})
        out = parse_verify_output(raw)
        self.assertTrue(out["passed"])

    def test_envelope_with_inner_fenced_json(self):
        inner = "```json\n{\"passed\": true, \"explanation\": \"fenced\"}\n```"
        raw = json.dumps({"result": inner})
        out = parse_verify_output(raw)
        self.assertTrue(out["passed"])
        self.assertEqual(out["explanation"], "fenced")

    def test_envelope_with_inner_prose_plus_json(self):
        inner = "Here's the verdict: {\"passed\": false, \"explanation\": \"nope\"}"
        raw = json.dumps({"result": inner})
        out = parse_verify_output(raw)
        self.assertFalse(out["passed"])
        self.assertEqual(out["explanation"], "nope")

    def test_fenced_json_no_envelope(self):
        raw = "```\n{\"passed\": true, \"explanation\": \"plain fence\"}\n```"
        out = parse_verify_output(raw)
        self.assertTrue(out["passed"])

    def test_fenced_json_missing_trailing_fence(self):
        # The parser's fence-stripper handles the case where the model
        # opened a fence but forgot to close it.
        raw = "```json\n{\"passed\": true, \"explanation\": \"open fence\"}"
        out = parse_verify_output(raw)
        self.assertTrue(out["passed"])

    def test_prose_with_embedded_json(self):
        raw = ("The acceptance criterion is satisfied because "
               "the function returns the expected value. "
               '{"passed": true, "explanation": "returns hi"}')
        out = parse_verify_output(raw)
        self.assertTrue(out["passed"])

    def test_completely_unparseable_returns_failed(self):
        out = parse_verify_output("nothing to see here, just prose")
        self.assertFalse(out["passed"])
        self.assertEqual(out["explanation"], "Unparseable output")

    def test_empty_string_returns_failed(self):
        out = parse_verify_output("")
        self.assertFalse(out["passed"])

    def test_json_list_returns_failed(self):
        # A top-level JSON list is valid JSON but not the expected shape.
        out = parse_verify_output('[1, 2, 3]')
        self.assertFalse(out["passed"])

    def test_envelope_without_result_or_passed(self):
        # An envelope with unexpected keys falls through to raw-stdout
        # prose parsing, which finds no JSON and returns failed.
        raw = json.dumps({"other": "value"})
        out = parse_verify_output(raw)
        self.assertFalse(out["passed"])

    def test_malformed_inner_json_falls_back_to_failed(self):
        inner = "not actually json {but has braces"
        raw = json.dumps({"result": inner})
        out = parse_verify_output(raw)
        self.assertFalse(out["passed"])


class TestBuildPrompts(unittest.TestCase):
    """#251: snapshot tests for build_step_prompt and build_verify_prompt.

    These functions render the literal text the Claude subprocess sees.
    A silent typo or accidentally-dropped section wouldn't surface until
    a live runner run produced garbage, so we pin the full string for
    one canonical input of every action + a verify step. Any future
    edit to the builders requires a deliberate test update."""

    def _base_step(self, action, step_number=1):
        return {
            "step_number": step_number,
            "type": action,
            "action": action,
            "description": "Add hello helper",
            "files": ["runner/hello.py", "runner/__init__.py"],
            "depends_on": [],
            "code_spec": "def hello():\n    return 'hi'",
        }

    def test_create_step_prompt_snapshot(self):
        prompt = build_step_prompt(self._base_step("create"), spec={})
        expected = (
            "You are implementing step 1 of a plan.\n"
            "\n"
            "## Step: Add hello helper\n"
            "**Type:** create\n"
            "**Action:** create\n"
            "**Files:** runner/hello.py, runner/__init__.py\n"
            "\n"
            "## Code specification\n"
            "\n"
            "def hello():\n    return 'hi'\n"
            "\n"
            "## Instructions\n"
            "\n"
            "Create the file(s) listed above with the implementation "
            "described in the code specification.\n"
            "\n"
            "Write the actual code — not pseudocode, not explanations. "
            "Just implement it.\n"
            "Use the existing codebase patterns and conventions."
        )
        self.assertEqual(prompt, expected)

    def test_modify_step_instruction_line(self):
        prompt = build_step_prompt(self._base_step("modify"), spec={})
        self.assertIn(
            "Read the existing file(s) listed above and apply the "
            "changes described in the code specification.",
            prompt,
        )
        self.assertIn("**Action:** modify", prompt)

    def test_delete_step_instruction_line(self):
        prompt = build_step_prompt(self._base_step("delete"), spec={})
        self.assertIn(
            "Delete the file(s) listed above as described in the code "
            "specification.",
            prompt,
        )

    def test_unknown_action_has_no_action_specific_instruction(self):
        # build_step_prompt silently drops the action-specific line for
        # non-create/modify/delete actions. Document the behavior so
        # any future contract change is deliberate.
        prompt = build_step_prompt(self._base_step("verify"), spec={})
        self.assertNotIn("Create the file(s)", prompt)
        self.assertNotIn("Read the existing file(s)", prompt)
        self.assertNotIn("Delete the file(s)", prompt)
        # The generic trailer is still present.
        self.assertIn("Write the actual code", prompt)

    def test_verify_prompt_snapshot(self):
        step = {
            "step_number": 3,
            "type": "verify",
            "action": "verify",
            "description": "hello returns 'hi'",
            "files": [],
            "depends_on": [1],
            "code_spec": "Call hello() and check the return value equals 'hi'.",
        }
        prompt = build_verify_prompt(step, spec={})
        expected = (
            "You are verifying step 3 of a plan.\n"
            "\n"
            "## Verification: hello returns 'hi'\n"
            "\n"
            "## What to check\n"
            "Call hello() and check the return value equals 'hi'.\n"
            "\n"
            "## Instructions\n"
            "\n"
            "Read the relevant files and confirm whether the acceptance "
            "criterion is met.\n"
            'Output ONLY a JSON object: {"passed": true/false, '
            '"explanation": "brief reason"}'
        )
        self.assertEqual(prompt, expected)


class TestExecutorEndToEnd(unittest.TestCase):
    """#250: real git repo + real validate_plan + real executor.main(),
    with only execute_step mocked (since we can't spawn Claude in a
    unit test). This is the single suite that exercises the full
    validate → checkout → execute → commit → log chain against live
    git subcommands and a real filesystem."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        os.chdir(self.repo)
        # Init a minimal repo with a baseline commit on master.
        self._git("init", "--initial-branch=master")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("x\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        # Isolate EXECUTIONS_DIR so we don't touch the real runner/executions/.
        self._executions = self.repo / "executions"
        self._executions.mkdir()
        self._executions_patch = mock.patch.object(
            executor, "EXECUTIONS_DIR", self._executions,
        )
        self._executions_patch.start()
        # Also patch _REPO_ROOT used by validate_plan's allowlist so the
        # plan's files= resolve under this temp repo, not the real one.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import validate_plan
        self._validate_plan = validate_plan
        self._repo_root_patch = mock.patch.object(
            validate_plan, "_REPO_ROOT", self.repo,
        )
        self._repo_root_patch.start()

    def tearDown(self):
        self._repo_root_patch.stop()
        self._executions_patch.stop()
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(self.repo), capture_output=True, text=True, check=True,
        )

    def _write_plan(self, uuid="e2e-test"):
        plan = {
            "uuid": uuid,
            "valid": True,
            "executor_model": "sonnet",
            "spec": {"acceptance_criteria": ["create the hello module"]},
            "steps": [
                {
                    "step_number": 1,
                    "type": "create",
                    "description": "create the hello module",
                    "action": "create",
                    "files": ["hello.py"],
                    "depends_on": [],
                    "code_spec": "print hello world",
                },
            ],
        }
        plan_path = self.repo / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return plan_path, plan

    def test_validate_then_execute_produces_branch_commit_and_log(self):
        plan_path, plan = self._write_plan("e2e-test")

        # Run real validate_plan on the real file. Must return 0.
        vp_result = subprocess.run(
            [sys.executable, "-m", "validate_plan", str(plan_path)],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True,
        )
        # Either runs as a module or we call the function directly — let's
        # just call the function since cwd is finicky.
        errors = self._validate_plan.validate(plan)
        self.assertEqual(errors, [], f"plan should validate: {errors}")

        # Mock execute_step to actually write the target file, then
        # return passed. This is the fidelity-vs-isolation tradeoff:
        # we can't spawn Claude in a unit test, but we can make sure
        # every other part of main() runs for real.
        def fake_execute(step, spec, model):
            for f in step.get("files", []):
                (self.repo / f).write_text("print('hello world')\n",
                                           encoding="utf-8")
            return {
                "step_number": step["step_number"],
                "status": "passed",
                "files_changed": step.get("files", []),
                "error": None,
            }

        argv = ["executor.py", str(plan_path)]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(executor, "execute_step", side_effect=fake_execute),
        ):
            try:
                executor.main()
            except SystemExit as e:
                self.assertEqual(e.code, 0, f"executor aborted: code={e.code}")

        # --- assertions ---
        # Branch exists and is checked out.
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(self.repo), capture_output=True, text=True,
        )
        self.assertEqual(head.stdout.strip(), "runner/e2e-test")

        # Commit exists with the expected subject.
        log = subprocess.run(
            ["git", "log", "--format=%s", "-n", "1"],
            cwd=str(self.repo), capture_output=True, text=True,
        )
        self.assertIn("runner/e2e-test step 1", log.stdout)

        # Target file is on disk.
        self.assertTrue((self.repo / "hello.py").exists())

        # Execution log has the right shape.
        log_path = self._executions / "e2e-test.json"
        self.assertTrue(log_path.exists())
        data = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["uuid"], "e2e-test")
        self.assertEqual(data["branch"], "runner/e2e-test")
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(data["steps"][0]["status"], "passed")
        self.assertEqual(data["steps"][0]["step_number"], 1)


class TestAssertFilesClean(unittest.TestCase):
    """#235: assert_files_clean is a pre-step guard against pre-existing
    dirty worktree state polluting a runner commit. If the user has
    uncommitted changes to a file a step targets, the runner must refuse
    to run rather than silently stealing those changes into its commit."""

    def _fake_git(self, stdout="", returncode=0):
        def _run(*args):
            return subprocess.CompletedProcess(
                args=("git",) + args, returncode=returncode,
                stdout=stdout, stderr="",
            )
        return _run

    def test_empty_files_is_noop(self):
        with mock.patch.object(executor, "git", side_effect=AssertionError(
                "git should not be called for empty file list")):
            assert_files_clean([])  # must not raise

    def test_clean_files_pass(self):
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git(stdout="")):
            assert_files_clean(["runner/foo.py"])  # no raise

    def test_dirty_files_raise(self):
        dirty = "diff --git a/runner/foo.py b/runner/foo.py\n...\n"
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git(stdout=dirty)):
            with self.assertRaises(RuntimeError) as ctx:
                assert_files_clean(["runner/foo.py"])
        self.assertIn("uncommitted changes", str(ctx.exception))
        self.assertIn("runner/foo.py", str(ctx.exception))

    def test_whitespace_only_stdout_is_clean(self):
        # git diff returns an empty diff as literally empty output, but
        # guard against ever seeing just whitespace (trailing newline)
        # and treating it as dirty.
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git(stdout="\n")):
            assert_files_clean(["runner/foo.py"])  # no raise

    def test_diff_failure_does_not_block(self):
        # If `git diff` itself fails (corrupt repo, detached state, etc.)
        # we fall through silently rather than blocking the runner — the
        # existing "subprocess made no changes" check will catch genuine
        # problems later. Don't let this guard become a new failure mode.
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git(stdout="", returncode=128)):
            assert_files_clean(["runner/foo.py"])  # no raise


class TestAssertNoUnexpectedWrites(unittest.TestCase):
    """#236: catch subprocess writes to paths the step did not declare.
    The pre/post snapshot is a set of `git status --porcelain=v1` lines,
    and set-difference isolates only the changes the step introduced."""

    def test_no_changes_passes(self):
        assert_no_unexpected_writes(set(), set(), ["runner/foo.py"])

    def test_expected_file_passes(self):
        post = {" M runner/foo.py"}
        assert_no_unexpected_writes(set(), post, ["runner/foo.py"])

    def test_unexpected_file_raises(self):
        post = {" M README.md"}
        with self.assertRaises(RuntimeError) as ctx:
            assert_no_unexpected_writes(set(), post, ["runner/foo.py"])
        self.assertIn("README.md", str(ctx.exception))
        self.assertIn("unexpected path", str(ctx.exception))

    def test_pre_dirty_file_not_flagged(self):
        # A file that was already dirty before the step and is still in
        # the same state afterward must not be reported — set-difference
        # removes it.
        pre = {" M unrelated.md"}
        post = {" M unrelated.md", " M runner/foo.py"}
        assert_no_unexpected_writes(pre, post, ["runner/foo.py"])

    def test_pre_dirty_file_further_modified_is_flagged(self):
        # If the file was staged before (M ) and then additionally
        # modified in the worktree (MM), the post line differs and it
        # shows up as new — correctly caught as an unexpected write
        # since the step did not declare it.
        pre = {"M  unrelated.md"}
        post = {"MM unrelated.md", " M runner/foo.py"}
        with self.assertRaises(RuntimeError) as ctx:
            assert_no_unexpected_writes(pre, post, ["runner/foo.py"])
        self.assertIn("unrelated.md", str(ctx.exception))

    def test_untracked_new_file_flagged(self):
        post = {"?? runner/garbage.json"}
        with self.assertRaises(RuntimeError) as ctx:
            assert_no_unexpected_writes(set(), post, ["runner/foo.py"])
        self.assertIn("runner/garbage.json", str(ctx.exception))

    def test_renamed_file_destination_checked(self):
        # Rename records have the form 'R  orig -> new'. The destination
        # is the path that was actually written.
        post = {"R  runner/old.py -> runner/new.py"}
        # Declared step.files is the new path → pass
        assert_no_unexpected_writes(set(), post, ["runner/new.py"])
        # Declared step.files is the old path → fail (new is unexpected)
        with self.assertRaises(RuntimeError):
            assert_no_unexpected_writes(set(), post, ["runner/old.py"])

    def test_path_normalization_across_slash_styles(self):
        # Porcelain always uses forward slashes; step.files may use
        # either. Normalization must accept both.
        post = {" M runner/foo.py"}
        assert_no_unexpected_writes(set(), post, ["runner\\foo.py"])

    def test_multiple_unexpected_files_all_reported(self):
        post = {" M README.md", " M core/session.py"}
        with self.assertRaises(RuntimeError) as ctx:
            assert_no_unexpected_writes(set(), post, ["runner/foo.py"])
        msg = str(ctx.exception)
        self.assertIn("README.md", msg)
        self.assertIn("core/session.py", msg)


class TestAssertActionMatchesStaged(unittest.TestCase):
    """#237: if a step declares action=modify but the subprocess DELETES
    the target file, the old generic 'staged diff exists' check passes
    and the deletion silently ships. Cross-check the status codes."""

    def _fake_git(self, stdout, returncode=0):
        def _run(*args):
            return subprocess.CompletedProcess(
                args=("git",) + args, returncode=returncode,
                stdout=stdout, stderr="",
            )
        return _run

    def test_modify_with_modified_passes(self):
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git("M\trunner/foo.py\n")):
            _assert_action_matches_staged("modify", ["runner/foo.py"])

    def test_modify_with_deletion_raises(self):
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git("D\trunner/foo.py\n")):
            with self.assertRaises(RuntimeError) as ctx:
                _assert_action_matches_staged("modify", ["runner/foo.py"])
        msg = str(ctx.exception)
        self.assertIn("modify", msg)
        self.assertIn("runner/foo.py", msg)
        self.assertIn("'D'", msg)

    def test_create_with_addition_passes(self):
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git("A\trunner/new.py\n")):
            _assert_action_matches_staged("create", ["runner/new.py"])

    def test_create_with_modification_raises(self):
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git("M\trunner/existing.py\n")):
            with self.assertRaises(RuntimeError):
                _assert_action_matches_staged("create", ["runner/existing.py"])

    def test_delete_with_deletion_passes(self):
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git("D\trunner/gone.py\n")):
            _assert_action_matches_staged("delete", ["runner/gone.py"])

    def test_delete_with_modification_raises(self):
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git("M\trunner/oops.py\n")):
            with self.assertRaises(RuntimeError):
                _assert_action_matches_staged("delete", ["runner/oops.py"])

    def test_mixed_mismatches_all_reported(self):
        stdout = "M\trunner/a.py\nD\trunner/b.py\n"
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git(stdout)):
            with self.assertRaises(RuntimeError) as ctx:
                _assert_action_matches_staged("modify",
                                              ["runner/a.py", "runner/b.py"])
        msg = str(ctx.exception)
        self.assertIn("runner/b.py", msg)
        self.assertNotIn("a.py: staged as 'M'", msg)  # a.py is fine

    def test_non_modifying_action_noop(self):
        # test/verify/empty action strings should skip the check entirely.
        with mock.patch.object(executor, "git", side_effect=AssertionError(
                "git should not be called for non-modifying actions")):
            _assert_action_matches_staged("test", ["runner/foo.py"])
            _assert_action_matches_staged("", ["runner/foo.py"])

    def test_empty_files_noop(self):
        with mock.patch.object(executor, "git", side_effect=AssertionError(
                "git should not be called for empty files")):
            _assert_action_matches_staged("modify", [])

    def test_git_failure_falls_through(self):
        # If git diff itself fails, skip the check rather than blocking
        # the runner on a flaky git call.
        with mock.patch.object(executor, "git",
                               side_effect=self._fake_git("", returncode=128)):
            _assert_action_matches_staged("modify", ["runner/foo.py"])


class TestPersistExecutionLog(unittest.TestCase):
    """#243: the log file must be written atomically and reflect state
    after every mutation inside the run loop, not only once at the very
    end. The atomic pattern (write .tmp, rename) is what prevents a
    partial-JSON file from being left behind if the process dies
    mid-flush."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_roundtrip(self):
        log_path = self.tmp_path / "exec.json"
        payload = {"uuid": "u", "branch": "b", "status": "running", "steps": []}
        persist_execution_log(log_path, payload)
        data = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(data, payload)

    def test_overwrite_replaces_previous_content(self):
        log_path = self.tmp_path / "exec.json"
        persist_execution_log(log_path, {"steps": [{"n": 1}]})
        persist_execution_log(log_path, {"steps": [{"n": 1}, {"n": 2}]})
        data = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["steps"]), 2)

    def test_atomic_tmp_file_not_left_behind(self):
        log_path = self.tmp_path / "exec.json"
        persist_execution_log(log_path, {"steps": []})
        siblings = list(self.tmp_path.iterdir())
        self.assertEqual([p.name for p in siblings], ["exec.json"])

    def test_pretty_printed_json(self):
        # The file should be human-readable (indent=2) so hand-reconcile
        # on resume drift is easy.
        log_path = self.tmp_path / "exec.json"
        persist_execution_log(log_path, {"a": 1, "b": 2})
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("\n", text)


class TestValidateResumeState(unittest.TestCase):
    """#242: cross-validate git commits vs. prior execution log before
    resuming. Disagreement means either squashed history or a hand-fix
    commit between runs — both leave resume behavior undefined. Only
    modifying actions (create/modify/delete) are cross-checked because
    verify/test steps never commit by design."""

    def _plan(self, *step_specs):
        """Shorthand: _plan((1,'modify'), (2,'verify'), ...) -> list[dict]."""
        return [{"step_number": n, "action": a} for n, a in step_specs]

    def test_both_empty_passes(self):
        self.assertEqual(validate_resume_state(set(), {}, []), [])

    def test_consistent_passed_and_committed(self):
        plan = self._plan((1, "modify"), (2, "modify"))
        prior = {1: {"status": "passed"}, 2: {"status": "passed"}}
        self.assertEqual(validate_resume_state({1, 2}, prior, plan), [])

    def test_log_passed_but_commit_missing_errors(self):
        plan = self._plan((1, "modify"), (2, "modify"))
        prior = {1: {"status": "passed"}, 2: {"status": "passed"}}
        errors = validate_resume_state({1}, prior, plan)  # step 2 commit gone
        self.assertEqual(len(errors), 1)
        self.assertIn("Step 2", errors[0])
        self.assertIn("no matching commit", errors[0])

    def test_log_failed_but_commit_present_errors(self):
        plan = self._plan((1, "modify"))
        prior = {1: {"status": "failed", "error": "boom"}}
        errors = validate_resume_state({1}, prior, plan)
        self.assertEqual(len(errors), 1)
        self.assertIn("Step 1", errors[0])
        self.assertIn("commit matching this step exists", errors[0])

    def test_log_skipped_but_commit_present_errors(self):
        plan = self._plan((3, "create"))
        prior = {3: {"status": "skipped"}}
        errors = validate_resume_state({3}, prior, plan)
        self.assertEqual(len(errors), 1)
        self.assertIn("'skipped'", errors[0])

    def test_verify_step_passed_without_commit_is_not_flagged(self):
        # Verify/test steps never commit by design — the log carries
        # their 'passed' status forward but they're not in
        # completed_step_numbers. Must not false-positive.
        plan = self._plan((4, "verify"), (5, "test"))
        prior = {4: {"status": "passed"}, 5: {"status": "passed"}}
        self.assertEqual(validate_resume_state(set(), prior, plan), [])

    def test_unknown_step_action_treated_as_modifying(self):
        # If the plan doesn't list a step the log knows about (plan was
        # trimmed between runs, etc.), the action defaults to '' — not
        # in _NON_COMMITTING_ACTIONS — so it IS cross-checked. Safer to
        # flag than to silently accept a dangling log entry.
        prior = {9: {"status": "passed"}}
        errors = validate_resume_state(set(), prior, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Step 9", errors[0])

    def test_multiple_disagreements_all_reported(self):
        plan = self._plan((1, "modify"), (2, "modify"), (3, "modify"))
        prior = {
            1: {"status": "passed"},     # missing commit
            2: {"status": "failed"},     # commit exists
            3: {"status": "passed"},     # consistent
        }
        errors = validate_resume_state({2, 3}, prior, plan)
        self.assertEqual(len(errors), 2)
        msg = "\n".join(errors)
        self.assertIn("Step 1", msg)
        self.assertIn("Step 2", msg)

    def test_non_dict_entries_ignored(self):
        plan = self._plan((3, "modify"))
        prior = {1: "not-a-dict", 2: None, 3: {"status": "passed"}}
        # Only step 3 should be cross-checked — and it's consistent.
        self.assertEqual(validate_resume_state({3}, prior, plan), [])

    # #244: in-flight ('started') entries.

    def test_started_entry_always_flagged(self):
        # A 'started' entry means the previous run was interrupted
        # mid-step — always flagged regardless of git state.
        plan = self._plan((1, "modify"))
        prior = {1: {"status": "started"}}
        errors = validate_resume_state(set(), prior, plan)
        self.assertEqual(len(errors), 1)
        self.assertIn("interrupted mid-step", errors[0])

    def test_started_entry_flagged_for_verify_too(self):
        # Even verify/test actions: if they're 'started', the caller
        # had no chance to distinguish pass/fail, so resume must stop.
        plan = self._plan((2, "verify"))
        prior = {2: {"status": "started"}}
        errors = validate_resume_state(set(), prior, plan)
        self.assertEqual(len(errors), 1)
        self.assertIn("Step 2", errors[0])

    def test_started_and_regular_disagreement_both_reported(self):
        plan = self._plan((1, "modify"), (2, "modify"))
        prior = {
            1: {"status": "started"},
            2: {"status": "failed"},  # with commit present → drift
        }
        errors = validate_resume_state({2}, prior, plan)
        self.assertEqual(len(errors), 2)


if __name__ == "__main__":
    unittest.main()
