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


class LegacyPathsUnchangedTests(unittest.TestCase):
    """With APIARY_STATE_LAYOUT=legacy set, path helpers keep the historical layout."""

    def setUp(self):
        self._env_patch = mock.patch.dict(
            os.environ, {notes.STATE_LAYOUT_ENV: "legacy"}
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_notes_path_uses_projects_dir(self):
        path = notes.notes_path("some-project-key")
        self.assertEqual(
            path, notes.PROJECTS_DIR / "some-project-key" / "notes.jsonl"
        )

    def test_archive_path_uses_projects_dir(self):
        path = notes.archive_path("some-project-key")
        self.assertEqual(
            path, notes.PROJECTS_DIR / "some-project-key" / "notes_archive.jsonl"
        )

    def test_learnings_path_uses_projects_dir(self):
        path = notes.learnings_path("some-project-key")
        self.assertEqual(
            path, notes.PROJECTS_DIR / "some-project-key" / "learnings.jsonl"
        )


class RepoLayoutPathsTests(unittest.TestCase):
    """With the flag on, helpers resolve under <repo-root>/.apiary/scribe/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name).resolve()
        self._env_patch = mock.patch.dict(
            os.environ, {notes.STATE_LAYOUT_ENV: "repo"}
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_paths_resolve_under_repo_root(self):
        fake_root = self.tmp_path / "fake-repo"
        fake_root.mkdir()
        with mock.patch.object(notes, "_git_repo_root", return_value=fake_root):
            self.assertEqual(
                notes.notes_path("ignored"),
                fake_root / ".apiary" / "scribe" / "notes.jsonl",
            )
            self.assertEqual(
                notes.archive_path("ignored"),
                fake_root / ".apiary" / "scribe" / "notes_archive.jsonl",
            )
            self.assertEqual(
                notes.learnings_path("ignored"),
                fake_root / ".apiary" / "scribe" / "learnings.jsonl",
            )

    def test_project_key_is_ignored_in_repo_layout(self):
        fake_root = self.tmp_path / "fake-repo"
        fake_root.mkdir()
        with mock.patch.object(notes, "_git_repo_root", return_value=fake_root):
            a = notes.notes_path("key-a")
            b = notes.notes_path("key-b")
            self.assertEqual(a, b)

    def test_fallback_to_cwd_when_not_in_git_repo(self):
        non_repo = self.tmp_path / "not-a-repo"
        non_repo.mkdir()
        with mock.patch.object(notes, "_git_repo_root", return_value=None), \
                mock.patch("scribe.notes.Path.cwd", return_value=non_repo):
            self.assertEqual(
                notes.notes_path("ignored"),
                non_repo / ".apiary" / "scribe" / "notes.jsonl",
            )

    def test_start_kwarg_is_forwarded_to_git_rev_parse(self):
        """notes_path(start=X) must resolve from X, not from process cwd."""
        fake_root = self.tmp_path / "session-repo"
        fake_root.mkdir()

        seen_start = {}

        def _fake_root(start=None):
            seen_start["value"] = start
            return fake_root

        with mock.patch.object(notes, "_git_repo_root", side_effect=_fake_root):
            result = notes.notes_path("ignored", start=fake_root)

        self.assertEqual(
            result, fake_root / ".apiary" / "scribe" / "notes.jsonl"
        )
        self.assertEqual(seen_start["value"], fake_root)

    def test_start_kwarg_covers_all_three_path_helpers(self):
        fake_root = self.tmp_path / "session-repo"
        fake_root.mkdir()
        with mock.patch.object(notes, "_git_repo_root", return_value=fake_root):
            expected_dir = fake_root / ".apiary" / "scribe"
            self.assertEqual(
                notes.notes_path("x", start=fake_root),
                expected_dir / "notes.jsonl",
            )
            self.assertEqual(
                notes.archive_path("x", start=fake_root),
                expected_dir / "notes_archive.jsonl",
            )
            self.assertEqual(
                notes.learnings_path("x", start=fake_root),
                expected_dir / "learnings.jsonl",
            )


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
