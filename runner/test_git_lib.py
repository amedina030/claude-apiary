#!/usr/bin/env python3
"""Unit tests for runner/git_lib.py — the shared git helpers.

`git_lib` was created to end the drift between the copies of `git()` in
executor / auto_harden / approval, but only `git()` and `format_git_error()`
ever moved, and `auto_harden.branch_exists` promptly drifted: it never got
the `refs/heads/` fix (ATK-006) that `executor.branch_exists` did. These
tests pin the consolidated behaviour against a real repo.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import git_lib


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class GitLibRepoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.repo.mkdir()
        _run(self.repo, "init", "--initial-branch=master")
        _run(self.repo, "config", "user.email", "t@e.com")
        _run(self.repo, "config", "user.name", "T")
        (self.repo / "README.md").write_text("seed\n", encoding="utf-8")
        _run(self.repo, "add", "README.md")
        _run(self.repo, "commit", "-m", "init")

    def test_branch_exists_only_matches_local_heads(self):
        self.assertTrue(git_lib.branch_exists("master", cwd=self.repo))
        self.assertFalse(git_lib.branch_exists("nope", cwd=self.repo))

    def test_branch_exists_ignores_a_remote_tracking_ref(self):
        """ATK-006: a bare `rev-parse --verify <name>` also resolves
        refs/remotes/origin/<name>, so a deleted local branch with a
        surviving remote ref looked like it still existed."""
        _run(self.repo, "update-ref", "refs/remotes/origin/ghost", "master")
        self.assertFalse(git_lib.branch_exists("ghost", cwd=self.repo))

    def test_create_checkout_and_current_branch(self):
        git_lib.create_branch("runner/x", cwd=self.repo)
        self.assertEqual(git_lib.current_branch(cwd=self.repo), "runner/x")
        git_lib.checkout("master", cwd=self.repo)
        self.assertEqual(git_lib.current_branch(cwd=self.repo), "master")

    def test_checkout_missing_branch_raises_with_context(self):
        with self.assertRaises(RuntimeError) as ctx:
            git_lib.checkout("runner/missing", cwd=self.repo)
        self.assertIn("runner/missing", str(ctx.exception))

    def test_create_existing_branch_raises(self):
        git_lib.create_branch("runner/x", cwd=self.repo)
        git_lib.checkout("master", cwd=self.repo)
        with self.assertRaises(RuntimeError):
            git_lib.create_branch("runner/x", cwd=self.repo)

    def test_non_ascii_commit_subject_decodes(self):
        """review runner Bug 7: git echoes back LLM-authored subjects, and
        text mode without encoding= decodes them with the Windows ANSI
        codepage — UnicodeDecodeError on the first accented character."""
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        _run(self.repo, "add", "README.md")
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "Á → ünïcode subject"],
            check=True, capture_output=True,
        )
        out = git_lib.git("log", "--format=%s", "-1", cwd=self.repo).stdout
        self.assertIn("ünïcode", out)


class TestRunBranchFromEnv(unittest.TestCase):
    def test_defaults_to_the_uuid_branch(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(git_lib.RUNNER_BRANCH_ENV, None)
            self.assertEqual(git_lib.run_branch_from_env("abc"), "runner/abc")

    def test_orchestrator_named_branch_wins(self):
        with mock.patch.dict(
            os.environ, {git_lib.RUNNER_BRANCH_ENV: "runner/slug-abc"},
        ):
            self.assertEqual(git_lib.run_branch_from_env("abc"), "runner/slug-abc")

    def test_blank_env_falls_back(self):
        with mock.patch.dict(os.environ, {git_lib.RUNNER_BRANCH_ENV: "   "}):
            self.assertEqual(git_lib.run_branch_from_env("abc"), "runner/abc")


if __name__ == "__main__":
    unittest.main()
