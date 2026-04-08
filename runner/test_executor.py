#!/usr/bin/env python3
"""Tests for runner/executor.py helpers.

Stdlib unittest only (no pytest), per docs/standards/code-style.md.
Focused on pure helpers; full main() integration is exercised by live
runner runs.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock  # noqa: F401  (used by TestCommitOrVerifyFiles)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import executor
from executor import execute_step, load_previous_log


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


class _FakeGitResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = ()


class TestCommitOrVerifyFiles(unittest.TestCase):
    """#212: commit_or_verify_files splits the file list into in-repo
    (verified via git diff --cached) and outside-repo (verified via
    content-hash diff). Catches the two #212 failure modes:

    * absolute paths outside the worktree returning 'no changes' from
      git even when the subprocess wrote them correctly (#222), and
    * subprocess no-op'ing on either bucket without distinct error.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        # An "in-repo" file: write under REPO_ROOT/runner/_test_tmp/ —
        # we never commit anything here, git is mocked. Use a real
        # subdirectory so _path_is_outside_repo returns False.
        self.in_repo_dir = executor.REPO_ROOT / "runner" / "_test_tmp"
        self.in_repo_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_in_repo_dir)
        self.in_repo_file = self.in_repo_dir / "fixture.txt"
        self.in_repo_file.write_text("in-repo before", encoding="utf-8")

    def _cleanup_in_repo_dir(self):
        try:
            for p in self.in_repo_dir.iterdir():
                p.unlink()
            self.in_repo_dir.rmdir()
        except OSError:
            pass

    def test_path_classification(self):
        self.assertFalse(executor._path_is_outside_repo(str(self.in_repo_file)))
        self.assertTrue(executor._path_is_outside_repo(str(self.tmp_path / "f.txt")))

    def test_hash_file_missing_returns_empty(self):
        self.assertEqual(executor._hash_file(self.tmp_path / "nope.txt"), "")

    def test_hash_file_present_returns_digest(self):
        f = self.tmp_path / "x.txt"
        f.write_text("hello", encoding="utf-8")
        digest = executor._hash_file(f)
        self.assertEqual(len(digest), 64)  # sha256 hex
        self.assertNotEqual(digest, "")

    def test_hash_outside_files_filters_to_outside_only(self):
        outside = self.tmp_path / "out.txt"
        outside.write_text("hi", encoding="utf-8")
        snap = executor.hash_outside_files([
            str(self.in_repo_file),
            str(outside),
        ])
        self.assertNotIn(str(self.in_repo_file), snap)
        self.assertIn(str(outside), snap)

    def test_empty_files_list_is_noop(self):
        # Should not call git or raise
        with mock.patch.object(executor, "git") as fake_git:
            executor.commit_or_verify_files([], "msg")
            fake_git.assert_not_called()

    def test_all_outside_changed_skips_git(self):
        outside = self.tmp_path / "state.json"
        outside.write_text("v1", encoding="utf-8")
        prior = executor.hash_outside_files([str(outside)])
        outside.write_text("v2", encoding="utf-8")
        with mock.patch.object(executor, "git") as fake_git:
            executor.commit_or_verify_files([str(outside)], "msg", prior)
            fake_git.assert_not_called()

    def test_all_outside_unchanged_raises(self):
        outside = self.tmp_path / "state.json"
        outside.write_text("v1", encoding="utf-8")
        prior = executor.hash_outside_files([str(outside)])
        # Don't modify the file — content hash will match prior.
        with mock.patch.object(executor, "git") as fake_git:
            with self.assertRaises(RuntimeError) as cm:
                executor.commit_or_verify_files([str(outside)], "msg", prior)
            fake_git.assert_not_called()
        self.assertIn("no changes", str(cm.exception).lower())

    def test_outside_created_from_missing_counts_as_change(self):
        # File didn't exist before the step but exists after — empty
        # sentinel hash differs from the new content hash, so it must
        # count as changed.
        outside = self.tmp_path / "newly_created.json"
        # Snapshot before the file exists.
        prior = executor.hash_outside_files([str(outside)])
        self.assertEqual(prior[str(outside)], "")
        outside.write_text("first content", encoding="utf-8")
        with mock.patch.object(executor, "git") as fake_git:
            executor.commit_or_verify_files([str(outside)], "msg", prior)
            fake_git.assert_not_called()

    def test_in_repo_changed_commits_via_git(self):
        # Mock git to simulate: add succeeds, diff --cached returncode 1
        # (meaning there IS a staged diff), commit succeeds.
        def fake_git(*args):
            if args[0] == "diff":
                return _FakeGitResult(returncode=1)  # has diff
            return _FakeGitResult(returncode=0)  # add and commit succeed

        with mock.patch.object(executor, "git", side_effect=fake_git) as g:
            executor.commit_or_verify_files([str(self.in_repo_file)], "msg")
            # Three calls: add, diff --cached --quiet, commit
            self.assertEqual(g.call_count, 3)
            self.assertEqual(g.call_args_list[0].args[0], "add")
            self.assertEqual(g.call_args_list[1].args[0], "diff")
            self.assertEqual(g.call_args_list[2].args[0], "commit")

    def test_in_repo_unchanged_raises_before_commit(self):
        def fake_git(*args):
            if args[0] == "diff":
                return _FakeGitResult(returncode=0)  # no diff
            return _FakeGitResult(returncode=0)

        with mock.patch.object(executor, "git", side_effect=fake_git) as g:
            with self.assertRaises(RuntimeError) as cm:
                executor.commit_or_verify_files([str(self.in_repo_file)], "msg")
            # add + diff but no commit
            self.assertEqual(g.call_count, 2)
        self.assertIn("no changes", str(cm.exception).lower())

    def test_mixed_files_both_change(self):
        outside = self.tmp_path / "state.json"
        outside.write_text("v1", encoding="utf-8")
        prior = executor.hash_outside_files([str(outside)])
        outside.write_text("v2", encoding="utf-8")

        def fake_git(*args):
            if args[0] == "diff":
                return _FakeGitResult(returncode=1)  # has diff
            return _FakeGitResult(returncode=0)

        with mock.patch.object(executor, "git", side_effect=fake_git) as g:
            executor.commit_or_verify_files(
                [str(self.in_repo_file), str(outside)],
                "msg",
                prior,
            )
            # Git is called only for the in-repo bucket
            self.assertEqual(g.call_count, 3)
            for call in g.call_args_list:
                # No git call should reference the outside path
                self.assertNotIn(str(outside), call.args)


if __name__ == "__main__":
    unittest.main()
