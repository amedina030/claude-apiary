#!/usr/bin/env python3
"""Tests for runner/monolithic_executor.py.

The monolithic executor spawns ONE ``claude -p`` subprocess that handles the
whole plan and is expected to commit after each step. These tests mock
the subprocess call and use a real git repo to stage the commits the
model "would have" produced. Post-hoc reconstruction + post-condition
verification is what we actually test here — the subprocess itself is
uninteresting.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import auto_harden, monolithic_executor
from runner.schema_versions import (
    EXECUTION_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
)


def _git(repo: Path, *args):
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


class ChainedExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        os.chdir(self.repo)
        _git(self.repo, "init", "--initial-branch=master")
        _git(self.repo, "config", "user.email", "t@e.com")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "README.md").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "initial")
        self._executions = self.repo / "executions"
        self._executions.mkdir()
        self._executions_patch = mock.patch.object(
            monolithic_executor,
            "EXECUTIONS_DIR",
            self._executions,
        )
        self._executions_patch.start()
        from runner import validate_plan

        self._validate_plan = validate_plan
        self._repo_root_patch = mock.patch.object(
            validate_plan,
            "_REPO_ROOT",
            self.repo,
        )
        self._repo_root_patch.start()

    def tearDown(self):
        self._repo_root_patch.stop()
        self._executions_patch.stop()
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _two_step_plan(self, uuid="chain-test"):
        plan = {
            "schema_version": PLAN_SCHEMA_VERSION,
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
                    "code_spec": "write the module",
                    "post_conditions": [
                        {"type": "file_exists", "file": "hello.py"},
                    ],
                },
                {
                    "step_number": 2,
                    "type": "modify",
                    "description": "add a helper",
                    "action": "modify",
                    "files": ["hello.py"],
                    "depends_on": [1],
                    "code_spec": "add a helper function",
                    "post_conditions": [
                        {"type": "file_contains", "file": "hello.py", "text": "def helper"},
                    ],
                },
            ],
        }
        plan_path = self.repo / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return plan_path, plan

    def _run(self, plan_path, fake_side_effect):
        argv = ["monolithic_executor.py", str(plan_path)]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                monolithic_executor,
                "_spawn_claude",
                side_effect=fake_side_effect,
            ),
        ):
            try:
                monolithic_executor.main()
                return 0
            except SystemExit as e:
                return e.code

    def _run_capturing_stderr(self, plan_path, fake_side_effect):
        """Same as _run but also returns whatever the stage wrote to stderr."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = self._run(plan_path, fake_side_effect)
        return rc, buf.getvalue()


class TestChainedExecutorHappyPath(ChainedExecutorTestBase):
    def test_all_steps_commit_cleanly(self):
        plan_path, plan = self._two_step_plan("happy")
        errors = self._validate_plan.validate(plan)
        self.assertEqual(errors, [], f"plan should validate: {errors}")

        def fake_claude(prompt, *, timeout=None, model=None):
            # Simulate the model: write hello.py with helper and make two
            # commits matching the expected subject format.
            (self.repo / "hello.py").write_text(
                "# placeholder\n",
                encoding="utf-8",
            )
            _git(self.repo, "add", "hello.py")
            _git(self.repo, "commit", "-m", "runner/happy step 1: create the hello module")
            (self.repo / "hello.py").write_text(
                "# placeholder\ndef helper():\n    return 1\n",
                encoding="utf-8",
            )
            _git(self.repo, "add", "hello.py")
            _git(self.repo, "commit", "-m", "runner/happy step 2: add a helper")
            return 0, '{"ok": true}', ""

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 0)
        log = json.loads((self._executions / "happy.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "completed")
        self.assertEqual([s["status"] for s in log["steps"]], ["passed", "passed"])
        self.assertEqual(log["mode"], "monolithic")
        self.assertEqual(log["schema_version"], EXECUTION_SCHEMA_VERSION)

    def test_second_step_subsumed_when_first_did_the_work(self):
        # Model writes hello.py (with helper) in step 1 and commits; step 2
        # is intentionally skipped (no commit) because the work is done.
        # Post-conditions for step 2 are already satisfied → step 2 marked
        # passed via subsumed_by="no_commit".
        plan_path, plan = self._two_step_plan("subsume-chain")
        errors = self._validate_plan.validate(plan)
        self.assertEqual(errors, [], f"plan should validate: {errors}")

        def fake_claude(prompt, *, timeout=None, model=None):
            (self.repo / "hello.py").write_text(
                "def helper():\n    return 1\n",
                encoding="utf-8",
            )
            _git(self.repo, "add", "hello.py")
            _git(self.repo, "commit", "-m", "runner/subsume-chain step 1: create the hello module")
            return 0, '{"ok": true}', ""

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 0)
        log = json.loads((self._executions / "subsume-chain.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "completed")
        self.assertEqual(log["steps"][0]["status"], "passed")
        self.assertEqual(log["steps"][1]["status"], "passed")
        self.assertEqual(log["steps"][1].get("subsumed_by"), "no_commit")


class TestChainedExecutorFailures(ChainedExecutorTestBase):
    def test_subprocess_nonzero_rc_aborts_run(self):
        plan_path, _ = self._two_step_plan("rc-fail")

        def fake_claude(prompt, *, timeout=None, model=None):
            return -1, "", "claude subprocess timed out after 1800s"

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 1)
        log = json.loads((self._executions / "rc-fail.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "aborted")
        self.assertIn("rc=-1", log["error"])
        # Transcript file preserved for debugging.
        self.assertTrue((self._executions / "rc-fail.transcript.json").exists())

    def test_unmet_post_condition_fails_step(self):
        plan_path, _ = self._two_step_plan("pc-unmet")

        def fake_claude(prompt, *, timeout=None, model=None):
            # Step 1 commits but WITHOUT the helper text that step 2's
            # post_condition expects. Step 1's own PC (file_exists) is
            # satisfied, so step 1 passes. Step 2 has no commit + unmet PC
            # → fails.
            (self.repo / "hello.py").write_text(
                "# no helper here\n",
                encoding="utf-8",
            )
            _git(self.repo, "add", "hello.py")
            _git(self.repo, "commit", "-m", "runner/pc-unmet step 1: create the hello module")
            return 0, '{"ok": true}', ""

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 1)
        log = json.loads((self._executions / "pc-unmet.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "aborted")
        self.assertEqual(log["steps"][0]["status"], "passed")
        self.assertEqual(log["steps"][1]["status"], "failed")
        self.assertIn("def helper", log["steps"][1]["error"])

    def test_commit_touching_undeclared_file_marks_step_failed(self):
        plan_path, _ = self._two_step_plan("leak")

        def fake_claude(prompt, *, timeout=None, model=None):
            # Step 1 commits hello.py AND README.md, which isn't in
            # step.files. Post-hoc check catches the leak.
            (self.repo / "hello.py").write_text("x\n", encoding="utf-8")
            (self.repo / "README.md").write_text("leaked\n", encoding="utf-8")
            _git(self.repo, "add", "hello.py", "README.md")
            _git(self.repo, "commit", "-m", "runner/leak step 1: create the hello module")
            # Step 2 commits cleanly so step-level test doesn't confound.
            (self.repo / "hello.py").write_text(
                "def helper():\n    return 1\n",
                encoding="utf-8",
            )
            _git(self.repo, "add", "hello.py")
            _git(self.repo, "commit", "-m", "runner/leak step 2: add a helper")
            return 0, '{"ok": true}', ""

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 1)
        log = json.loads((self._executions / "leak.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "aborted")
        self.assertEqual(log["steps"][0]["status"], "failed")
        self.assertIn("outside step.files", log["steps"][0]["error"])

    def test_missing_step_commit_with_no_pc_marks_failed(self):
        # Plan step has no post_conditions; if the model skips committing,
        # we can't prove via PCs that it was intentionally subsumed, so we
        # must fail it rather than silently pass.
        plan = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "uuid": "noskip",
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
                    "code_spec": "make it",
                },
            ],
        }
        plan_path = self.repo / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        def fake_claude(prompt, *, timeout=None, model=None):
            # Do nothing — no commits.
            return 0, '{"ok": true}', ""

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 1)
        log = json.loads((self._executions / "noskip.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "aborted")
        self.assertEqual(log["steps"][0]["status"], "failed")


class TestSchemaVersionSeam(ChainedExecutorTestBase):
    """Review runner Bug 1: the artifact this stage writes must carry the
    version the next stage asserts, and the plan it consumes must carry the
    one this stage expects."""

    def test_aborted_log_also_carries_schema_version(self):
        # The log is persisted once before the subprocess runs and again
        # after it fails; both writes must be version-stamped, or --resume
        # hands auto_harden an unreadable artifact.
        plan_path, _ = self._two_step_plan("abort-schema")

        def fake_claude(prompt, *, timeout=None, model=None):
            return -1, "", "boom"

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 1)
        log = json.loads((self._executions / "abort-schema.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "aborted")
        self.assertEqual(log["schema_version"], EXECUTION_SCHEMA_VERSION)

    def test_plan_without_schema_version_is_rejected(self):
        plan_path, plan = self._two_step_plan("no-plan-version")
        plan.pop("schema_version")
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        def fake_claude(prompt, *, timeout=None, model=None):
            raise AssertionError("claude must not be spawned for a bad plan")

        rc, err = self._run_capturing_stderr(plan_path, fake_claude)
        self.assertEqual(rc, 1)
        self.assertIn("schema_version=None", err)
        self.assertIn("plan", err)
        # Nothing was written — the stage bailed before touching git.
        self.assertFalse((self._executions / "no-plan-version.json").exists())

    def test_plan_with_future_schema_version_is_rejected(self):
        plan_path, plan = self._two_step_plan("future-plan-version")
        plan["schema_version"] = PLAN_SCHEMA_VERSION + 1
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        def fake_claude(prompt, *, timeout=None, model=None):
            raise AssertionError("claude must not be spawned for a bad plan")

        rc, err = self._run_capturing_stderr(plan_path, fake_claude)
        self.assertEqual(rc, 1)
        self.assertIn(f"schema_version={PLAN_SCHEMA_VERSION + 1}", err)


class TestMonolithicArtifactFeedsAutoHarden(ChainedExecutorTestBase):
    """The stage-4 → stage-5 seam, end to end with two real stage mains.

    Before the schema_version fix this pairing was untested and broken:
    every default-mode run reached auto_harden and died on
    ``SchemaVersionError``. The LLM subprocess is the only thing mocked.
    """

    def setUp(self):
        super().setUp()
        self._plans = self.repo / "plans"
        self._plans.mkdir()
        self._hardens = self.repo / "hardens"
        self._hardens.mkdir()
        self._hardens_patch = mock.patch.object(
            auto_harden,
            "HARDENS_DIR",
            self._hardens,
        )
        self._hardens_patch.start()
        self._plans_patch = mock.patch.object(
            auto_harden,
            "plans_dir",
            lambda *a, **k: self._plans,
        )
        self._plans_patch.start()

    def tearDown(self):
        self._plans_patch.stop()
        self._hardens_patch.stop()
        super().tearDown()

    def _execute_plan(self, uuid):
        """Run stage 4 to completion; return the execution-log path."""
        plan_path, plan = self._two_step_plan(uuid)
        # auto_harden re-opens the plan for the spec, keyed by uuid.
        (self._plans / f"{uuid}.json").write_text(
            json.dumps(plan),
            encoding="utf-8",
        )

        def fake_claude(prompt, *, timeout=None, model=None):
            (self.repo / "hello.py").write_text(
                "def helper():\n    return 1\n",
                encoding="utf-8",
            )
            _git(self.repo, "add", "hello.py")
            _git(self.repo, "commit", "-m", f"runner/{uuid} step 1: create the hello module")
            (self.repo / "hello.py").write_text(
                "def helper():\n    return 2\n",
                encoding="utf-8",
            )
            _git(self.repo, "add", "hello.py")
            _git(self.repo, "commit", "-m", f"runner/{uuid} step 2: add a helper")
            return 0, '{"ok": true}', ""

        rc = self._run(plan_path, fake_claude)
        self.assertEqual(rc, 0, "stage 4 must succeed before stage 5 runs")
        return self._executions / f"{uuid}.json"

    def _run_auto_harden(self, log_path):
        """Run stage 5 with a fake attacker that finds nothing."""

        def fake_claude(prompt, model=None, timeout=None):
            return 0, json.dumps({"result": "[]"}), ""

        argv = ["auto_harden.py", str(log_path)]
        buf = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(auto_harden, "run_claude", side_effect=fake_claude),
            contextlib.redirect_stderr(buf),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                auto_harden.main()
                return 0, buf.getvalue()
            except SystemExit as e:
                return (e.code or 0), buf.getvalue()

    def test_auto_harden_accepts_the_monolithic_execution_artifact(self):
        log_path = self._execute_plan("seam-ok")
        log = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(log["schema_version"], EXECUTION_SCHEMA_VERSION)
        self.assertEqual(log["status"], "completed")

        rc, err = self._run_auto_harden(log_path)
        self.assertEqual(rc, 0, f"auto_harden rejected the artifact: {err}")
        self.assertNotIn("schema_version", err)

        result = json.loads((self._hardens / "seam-ok.json").read_text(encoding="utf-8"))
        self.assertEqual(result["verdict"], "all_resolved")
        self.assertEqual(result["branch"], "runner/seam-ok")
        # It read the real changed-file list off the monolithic artifact.
        self.assertEqual(
            [s["files_changed"] for s in log["steps"]],
            [["hello.py"], ["hello.py"]],
        )

    def test_auto_harden_still_rejects_an_unstamped_artifact(self):
        # Negative control: proves the test above passes because of the
        # stamp, not because auto_harden stopped checking.
        log_path = self._execute_plan("seam-unstamped")
        log = json.loads(log_path.read_text(encoding="utf-8"))
        log.pop("schema_version")
        log_path.write_text(json.dumps(log), encoding="utf-8")

        rc, err = self._run_auto_harden(log_path)
        self.assertEqual(rc, 1)
        self.assertIn("schema_version=None", err)
        self.assertFalse((self._hardens / "seam-unstamped.json").exists())


class TestReconstructionHelpers(ChainedExecutorTestBase):
    def test_commits_since_base_filters_and_parses(self):
        _git(self.repo, "checkout", "-b", "runner/helper-test")
        (self.repo / "a.py").write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "a.py")
        _git(self.repo, "commit", "-m", "runner/helper-test step 1: first")
        (self.repo / "a.py").write_text("xy\n", encoding="utf-8")
        _git(self.repo, "add", "a.py")
        _git(self.repo, "commit", "-m", "unrelated commit")

        commits = monolithic_executor._commits_since_base("master")
        subjects = [s for _, s in commits]
        self.assertEqual(len(commits), 2)
        self.assertIn("runner/helper-test step 1: first", subjects)

    def test_detect_global_unexpected_writes(self):
        plan = {
            "steps": [
                {"files": ["a.py"]},
                {"files": ["b.py"]},
            ],
        }
        _git(self.repo, "checkout", "-b", "runner/leak-test")
        for f in ("a.py", "b.py", "c.py"):
            (self.repo / f).write_text("x\n", encoding="utf-8")
        _git(self.repo, "add", "a.py", "b.py", "c.py")
        _git(self.repo, "commit", "-m", "chunky")
        unexpected = monolithic_executor.detect_global_unexpected_writes(
            plan,
            "master",
        )
        self.assertEqual(unexpected, ["c.py"])


if __name__ == "__main__":
    unittest.main()
