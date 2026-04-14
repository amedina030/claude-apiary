#!/usr/bin/env python3
"""Tests for scribe state-directory resolution (todo #262, decision #269).

Covers the APIARY_STATE_LAYOUT=repo opt-in that points scribe at
<git-repo-root>/.apiary/scribe/ instead of the legacy
~/.claude/projects/<project_key>/ path.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scribe.notes as notes


class StateLayoutFlagTests(unittest.TestCase):
    """_use_repo_layout() reads APIARY_STATE_LAYOUT from the environment.

    Default flipped in todo #268: env unset → repo layout. ``legacy`` is
    the explicit escape hatch back to the old ~/.claude/projects/ layout.
    """

    def test_default_is_repo(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(notes.STATE_LAYOUT_ENV, None)
            self.assertTrue(notes._use_repo_layout())

    def test_repo_value_enables_layout(self):
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "repo"}):
            self.assertTrue(notes._use_repo_layout())

    def test_value_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "LEGACY"}):
            self.assertFalse(notes._use_repo_layout())

    def test_whitespace_tolerated(self):
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "  legacy  "}):
            self.assertFalse(notes._use_repo_layout())

    def test_legacy_value_disables_layout(self):
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "legacy"}):
            self.assertFalse(notes._use_repo_layout())

    def test_unrelated_value_falls_through_to_repo(self):
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "something-else"}):
            self.assertTrue(notes._use_repo_layout())


class ScribeStateDirTests(unittest.TestCase):
    """scribe_state_dir() is layout-aware and returns None in legacy mode."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_none_in_legacy_layout(self):
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "legacy"}):
            self.assertIsNone(notes.scribe_state_dir(self.tmp_path))

    def test_returns_repo_dir_in_repo_layout(self):
        fake_root = self.tmp_path / "repo"
        fake_root.mkdir()
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "repo"}), \
                mock.patch.object(notes, "_git_repo_root", return_value=fake_root):
            self.assertEqual(
                notes.scribe_state_dir(fake_root),
                fake_root / ".apiary" / "scribe",
            )

    def test_returns_none_when_repo_layout_and_not_in_git(self):
        non_repo = self.tmp_path / "not-a-repo"
        non_repo.mkdir()
        with mock.patch.dict(os.environ, {notes.STATE_LAYOUT_ENV: "repo"}), \
                mock.patch.object(notes, "_git_repo_root", return_value=None):
            self.assertIsNone(notes.scribe_state_dir(non_repo))


class GitRepoRootTests(unittest.TestCase):
    """_git_repo_root shells out to git and tolerates the absence of git."""

    def test_returns_none_when_git_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(notes._git_repo_root())

    def test_returns_none_on_nonzero_exit(self):
        fake_result = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="fatal: not a git repo"
        )
        with mock.patch("subprocess.run", return_value=fake_result):
            self.assertIsNone(notes._git_repo_root())

    def test_returns_path_on_success(self):
        fake_result = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="/some/repo\n", stderr=""
        )
        with mock.patch("subprocess.run", return_value=fake_result):
            self.assertEqual(notes._git_repo_root(), Path("/some/repo"))

    def test_returns_none_on_empty_stdout(self):
        fake_result = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="\n", stderr=""
        )
        with mock.patch("subprocess.run", return_value=fake_result):
            self.assertIsNone(notes._git_repo_root())

    def test_real_git_resolves_apiary_repo(self):
        """Smoke test against the real repo — this file lives in it."""
        root = notes._git_repo_root(Path(__file__).resolve().parent)
        self.assertIsNotNone(root)
        self.assertTrue((root / ".git").exists() or (root / ".git").is_file())


if __name__ == "__main__":
    unittest.main()
