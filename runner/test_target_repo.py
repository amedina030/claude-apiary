#!/usr/bin/env python3
"""Unit tests for runner/target_repo.py."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import target_repo


def _git_init(path: Path) -> None:
    """Initialize a bare-minimum git repo so resolve_target_repo accepts it."""
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True, capture_output=True,
    )


class TestChooseTargetRepo(unittest.TestCase):
    """Pure precedence picker — no I/O, no validation."""

    def test_default_is_apiary_repo_root(self):
        with mock.patch.object(target_repo, "cfg", return_value=None):
            chosen = target_repo.choose_target_repo()
        self.assertEqual(chosen, target_repo.APIARY_REPO_ROOT)

    def test_apiary_root_override(self):
        custom = Path("/tmp/some/other/apiary")
        with mock.patch.object(target_repo, "cfg", return_value=None):
            chosen = target_repo.choose_target_repo(apiary_root=custom)
        self.assertEqual(chosen, custom)

    def test_cli_override_wins_over_intake_and_config(self):
        intake = {"target_repo": "/from/intake"}
        with mock.patch.object(target_repo, "cfg", return_value="/from/config"):
            chosen = target_repo.choose_target_repo(
                cli_override="/from/cli", intake=intake,
            )
        self.assertEqual(chosen, Path("/from/cli"))

    def test_intake_field_wins_over_config(self):
        intake = {"target_repo": "/from/intake"}
        with mock.patch.object(target_repo, "cfg", return_value="/from/config"):
            chosen = target_repo.choose_target_repo(intake=intake)
        self.assertEqual(chosen, Path("/from/intake"))

    def test_config_used_when_no_cli_or_intake(self):
        with mock.patch.object(target_repo, "cfg", return_value="/from/config"):
            chosen = target_repo.choose_target_repo()
        self.assertEqual(chosen, Path("/from/config"))

    def test_intake_without_target_repo_field_falls_through(self):
        intake = {"id": "uuid-x", "title": "no target"}
        with mock.patch.object(target_repo, "cfg", return_value=None):
            chosen = target_repo.choose_target_repo(intake=intake)
        self.assertEqual(chosen, target_repo.APIARY_REPO_ROOT)

    def test_intake_with_blank_target_repo_falls_through(self):
        intake = {"target_repo": "   "}
        with mock.patch.object(target_repo, "cfg", return_value=None):
            chosen = target_repo.choose_target_repo(intake=intake)
        self.assertEqual(chosen, target_repo.APIARY_REPO_ROOT)

    def test_intake_with_non_string_target_repo_ignored(self):
        intake = {"target_repo": 12345}
        with mock.patch.object(target_repo, "cfg", return_value=None):
            chosen = target_repo.choose_target_repo(intake=intake)
        self.assertEqual(chosen, target_repo.APIARY_REPO_ROOT)

    def test_blank_config_falls_through(self):
        with mock.patch.object(target_repo, "cfg", return_value="   "):
            chosen = target_repo.choose_target_repo()
        self.assertEqual(chosen, target_repo.APIARY_REPO_ROOT)

    def test_cli_override_pathlike(self):
        p = Path("/explicit/path")
        with mock.patch.object(target_repo, "cfg", return_value=None):
            chosen = target_repo.choose_target_repo(cli_override=p)
        self.assertEqual(chosen, p)

    def test_intake_target_repo_string_is_stripped(self):
        intake = {"target_repo": "  /padded/path  "}
        with mock.patch.object(target_repo, "cfg", return_value=None):
            chosen = target_repo.choose_target_repo(intake=intake)
        self.assertEqual(chosen, Path("/padded/path"))


class TestResolveTargetRepo(unittest.TestCase):
    """Validating resolver — checks existence, dir-ness, and .git presence."""

    def test_resolves_real_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "scratch_repo"
            repo.mkdir()
            _git_init(repo)
            resolved = target_repo.resolve_target_repo(cli_override=repo)
            self.assertEqual(resolved, repo.resolve())

    def test_apiary_self_resolves_when_no_args(self):
        # The actual apiary checkout MUST resolve cleanly — this is the
        # backwards-compatibility guarantee for all current callers.
        with mock.patch.object(target_repo, "cfg", return_value=None):
            resolved = target_repo.resolve_target_repo()
        self.assertEqual(resolved, target_repo.APIARY_REPO_ROOT.resolve())

    def test_nonexistent_path_raises(self):
        bogus = Path(tempfile.gettempdir()) / "nonexistent-target-repo-xyz123"
        with self.assertRaises(ValueError) as ctx:
            target_repo.resolve_target_repo(cli_override=bogus)
        self.assertIn("does not exist", str(ctx.exception))

    def test_file_path_raises(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "not_a_dir"
            f.write_text("hello", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                target_repo.resolve_target_repo(cli_override=f)
            self.assertIn("not a directory", str(ctx.exception))

    def test_dir_without_git_raises(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "empty_dir"
            d.mkdir()
            with self.assertRaises(ValueError) as ctx:
                target_repo.resolve_target_repo(cli_override=d)
            self.assertIn("not a git repository", str(ctx.exception))

    def test_git_marker_can_be_a_file(self):
        # Worktrees and submodules use a .git FILE, not a dir. The
        # validator must accept that.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "git_via_file"
            d.mkdir()
            (d / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
            resolved = target_repo.resolve_target_repo(cli_override=d)
            self.assertEqual(resolved, d.resolve())


if __name__ == "__main__":
    unittest.main()
