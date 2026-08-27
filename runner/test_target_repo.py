#!/usr/bin/env python3
"""Unit tests for runner/target_repo.py."""
import os
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


class _EnvIsolation(unittest.TestCase):
    """Clear APIARY_TARGET_REPO before each test and restore it after.

    Tests that probe the precedence chain ``choose_target_repo`` /
    ``_default_target`` must not leak env-var state across the unittest
    runner's single process — otherwise a prior test that exercises
    ``set_active_target`` would silently change this class's fallback path.
    """

    def setUp(self):
        self.__prior = os.environ.pop("APIARY_TARGET_REPO", None)

    def tearDown(self):
        if self.__prior is None:
            os.environ.pop("APIARY_TARGET_REPO", None)
        else:
            os.environ["APIARY_TARGET_REPO"] = self.__prior


class TestChooseTargetRepo(_EnvIsolation):
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


class TestResolveTargetRepo(_EnvIsolation):
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


class TestActiveTargetEnv(unittest.TestCase):
    """APIARY_TARGET_REPO env var propagation."""

    def setUp(self):
        self._old = os.environ.pop(target_repo.ACTIVE_TARGET_ENV, None)

    def tearDown(self):
        if self._old is not None:
            os.environ[target_repo.ACTIVE_TARGET_ENV] = self._old
        else:
            os.environ.pop(target_repo.ACTIVE_TARGET_ENV, None)

    def test_set_active_target_publishes_env(self):
        target_repo.set_active_target(Path("/tmp/x"))
        self.assertEqual(
            os.environ[target_repo.ACTIVE_TARGET_ENV],
            str(Path("/tmp/x").resolve()),
        )

    def test_clear_active_target_removes_env(self):
        target_repo.set_active_target(Path("/tmp/x"))
        target_repo.clear_active_target()
        self.assertNotIn(target_repo.ACTIVE_TARGET_ENV, os.environ)

    def test_env_var_feeds_choose_target_repo(self):
        with tempfile.TemporaryDirectory() as td:
            target_repo.set_active_target(Path(td))
            with mock.patch.object(target_repo, "cfg", return_value=None):
                chosen = target_repo.choose_target_repo()
            self.assertEqual(chosen, Path(td).resolve())

    def test_env_var_feeds_path_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            target_repo.set_active_target(Path(td))
            self.assertEqual(
                target_repo.intake_dir(),
                Path(td).resolve() / ".apiary" / "runner" / "intake",
            )
            self.assertEqual(
                target_repo.artifacts_root(),
                Path(td).resolve() / ".apiary" / "runner",
            )

    def test_explicit_target_overrides_env(self):
        target_repo.set_active_target(Path("/tmp/env"))
        explicit = Path("/tmp/explicit")
        self.assertEqual(
            target_repo.intake_dir(explicit),
            explicit.resolve() / ".apiary" / "runner" / "intake",
        )

    def test_cli_override_beats_env(self):
        with tempfile.TemporaryDirectory() as td_env, tempfile.TemporaryDirectory() as td_cli:
            target_repo.set_active_target(Path(td_env))
            with mock.patch.object(target_repo, "cfg", return_value=None):
                chosen = target_repo.choose_target_repo(cli_override=Path(td_cli))
            self.assertEqual(chosen, Path(td_cli))


class TestArtifactPathHelpers(_EnvIsolation):
    """Path-construction helpers for the target-rooted runner layout."""

    def test_artifacts_root_under_target(self):
        t = Path("/tmp/some-target")
        self.assertEqual(
            target_repo.artifacts_root(t),
            t.resolve() / ".apiary" / "runner",
        )

    def test_artifacts_root_falls_back_to_apiary_root(self):
        self.assertEqual(
            target_repo.artifacts_root(None),
            target_repo.APIARY_REPO_ROOT / ".apiary" / "runner",
        )

    def test_named_subdirs_nest_under_artifacts_root(self):
        t = Path("/tmp/some-target").resolve()
        root = target_repo.artifacts_root(t)
        cases = [
            ("intake_dir", "intake"),
            ("backlog_dir", "backlog"),
            ("specs_dir", "specs"),
            ("plans_dir", "plans"),
            ("executions_dir", "executions"),
            ("hardens_dir", "hardens"),
            ("reports_dir", "reports"),
            ("locks_dir", "locks"),
            ("runs_dir", "runs"),
            ("logs_dir", "logs"),
        ]
        for fn_name, subdir in cases:
            with self.subTest(helper=fn_name):
                self.assertEqual(
                    getattr(target_repo, fn_name)(t),
                    root / subdir,
                )

    def test_run_history_is_a_file(self):
        t = Path("/tmp/some-target").resolve()
        root = target_repo.artifacts_root(t)
        self.assertEqual(target_repo.run_history_path(t), root / "run_history.jsonl")

    def test_worktrees_dir_sibling_of_runner(self):
        t = Path("/tmp/some-target").resolve()
        self.assertEqual(
            target_repo.worktrees_dir(t),
            t / ".apiary" / "runner-worktrees",
        )
        self.assertNotEqual(
            target_repo.worktrees_dir(t).parent,
            target_repo.artifacts_root(t),
        )


if __name__ == "__main__":
    unittest.main()
