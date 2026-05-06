"""Tests for incubator/cli.py.

Validation and helper logic are exercised directly with TemporaryDirectory.
The scribe interaction is monkey-patched so tests don't touch real apiary state.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from incubator import cli


class SlugifyTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(cli._slugify_dirname("food-tracker"), "food-tracker")

    def test_camel_and_spaces(self):
        self.assertEqual(cli._slugify_dirname("My Cool Project"), "my-cool-project")

    def test_strips_punctuation(self):
        self.assertEqual(cli._slugify_dirname("foo!@#bar"), "foo-bar")

    def test_empty_falls_back(self):
        self.assertEqual(cli._slugify_dirname(""), "project")
        self.assertEqual(cli._slugify_dirname("___"), "project")


class ParseGoalLinesTests(unittest.TestCase):
    def test_extracts_all_three(self):
        spec = (
            "## Goal\n"
            "- **Problem:** Bootstrapping is repetitive.\n"
            "- **Solution:** A skill that scaffolds new repos.\n"
            "- **Value:** Faster side-project starts.\n"
            "\n"
            "## Shape\n- something\n"
        )
        problem, solution, value = cli._parse_goal_lines(spec)
        self.assertEqual(problem, "Bootstrapping is repetitive.")
        self.assertEqual(solution, "A skill that scaffolds new repos.")
        self.assertEqual(value, "Faster side-project starts.")

    def test_missing_returns_empty(self):
        spec = "## Goal\n- **Problem:** only this one.\n"
        problem, solution, value = cli._parse_goal_lines(spec)
        self.assertEqual(problem, "only this one.")
        self.assertEqual(solution, "")
        self.assertEqual(value, "")


class ValidateTargetTests(unittest.TestCase):
    def test_relative_path_rejected(self):
        path, err = cli._validate_target("relative/dir")
        self.assertIsNone(path)
        self.assertIn("absolute", err)

    def test_empty_path_rejected(self):
        path, err = cli._validate_target("")
        self.assertIsNone(path)
        self.assertIn("empty", err)

    def test_existing_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            existing = Path(td) / "already-here"
            existing.mkdir()
            path, err = cli._validate_target(str(existing))
            self.assertIsNone(path)
            self.assertIn("already exists", err)

    def test_missing_parent_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "no-such-parent" / "child"
            path, err = cli._validate_target(str(target))
            self.assertIsNone(path)
            self.assertIn("parent directory", err)

    def test_inside_existing_git_repo_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            outer = Path(td)
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=str(outer),
                check=True,
                capture_output=True,
            )
            target = outer / "would-be-nested"
            path, err = cli._validate_target(str(target))
            self.assertIsNone(path)
            self.assertIn("inside an existing git repo", err)

    def test_valid_path_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "fresh-project"
            path, err = cli._validate_target(str(target))
            self.assertIsNone(err)
            self.assertEqual(path, target.resolve())


class SkeletonLayoutTests(unittest.TestCase):
    """End-to-end check that the skeleton lands correctly given a fake spec."""

    SAMPLE_SPEC = (
        "## Goal\n"
        "- **Problem:** Bootstrapping is repetitive.\n"
        "- **Solution:** Scaffold new repos in one shot.\n"
        "- **Value:** Faster side-project starts.\n"
        "\n"
        "## Shape\n- Component A: does the thing.\n"
    )

    def test_spawn_writes_full_skeleton(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "spawn-target"

            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="Test User <test@example.com>",
                session_id="test-session",
            )

            fake_add = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Added C-2026-1 (context)\n", stderr=""
            )
            fake_done = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Marked C-2026-999 done.\n", stderr=""
            )

            with mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)), \
                 mock.patch.object(cli, "_run_scribe", side_effect=[fake_add, fake_done]), \
                 mock.patch.object(cli, "_per_repo_bootstrap", return_value="mocked"):
                rc = cli.cmd_spawn(args)

            self.assertEqual(rc, cli.EXIT_OK, msg="spawn should succeed")
            self.assertTrue((target / ".git").is_dir(), "git init should produce .git/")
            self.assertTrue((target / ".gitignore").is_file())
            self.assertTrue((target / "pyproject.toml").is_file())
            self.assertTrue((target / "CLAUDE.md").is_file())
            self.assertTrue((target / ".apiary").is_dir())

            pyproject_text = (target / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn('name = "spawn-target"', pyproject_text)
            self.assertIn("Test User", pyproject_text)

            claude_text = (target / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("# spawn-target", claude_text)
            self.assertIn("Bootstrapping is repetitive.", claude_text)
            self.assertIn("Scaffold new repos in one shot.", claude_text)

    def test_spawn_rolls_back_on_git_init_failure(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "rollback-target"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            with mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)), \
                 mock.patch.object(cli, "_run_git_init", return_value=(False, "boom")), \
                 mock.patch.object(sys, "stderr", new_callable=io.StringIO):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_SPAWN_FAILED)
            self.assertFalse(target.exists(), "partial directory should be removed")

    def test_spawn_reports_partial_on_migration_failure(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "partial-target"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            fake_add_fail = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="scribe blew up"
            )
            with mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)), \
                 mock.patch.object(cli, "_run_scribe", return_value=fake_add_fail), \
                 mock.patch.object(sys, "stderr", new_callable=io.StringIO):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_MIGRATION_FAILED)
            self.assertTrue(target.exists(), "repo should be left intact for manual recovery")
            self.assertTrue((target / ".git").is_dir())


class SpecFetchErrorTests(unittest.TestCase):
    def test_missing_spec_returns_exit_3(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "x"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            with mock.patch.object(cli, "_fetch_spec", return_value=(None, "not found")), \
                 mock.patch.object(sys, "stderr", new_callable=io.StringIO):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_SPEC_NOT_FOUND)
            self.assertFalse(target.exists())


class PerRepoBootstrapTests(unittest.TestCase):
    """``_per_repo_bootstrap`` is best-effort — failures must not raise
    or affect the return value of cmd_spawn. They surface as a short
    status message in the spawn report instead."""

    def test_install_failure_returns_status_string(self):
        target = Path("/nonexistent/target")
        # Make install raise InstallError → wrapper returns "skipped (...)"
        from core import install as install_mod
        with mock.patch.object(
            install_mod, "install",
            side_effect=install_mod.InstallError("not a directory"),
        ):
            msg = cli._per_repo_bootstrap(target)
        self.assertIn("skipped", msg)
        self.assertIn("not a directory", msg)

    def test_unexpected_exception_returns_status_string(self):
        from core import install as install_mod
        with mock.patch.object(install_mod, "install", side_effect=RuntimeError("boom")):
            msg = cli._per_repo_bootstrap(Path("/x"))
        self.assertIn("skipped", msg)
        self.assertIn("RuntimeError", msg)

    def test_success_returns_uid_and_slug(self):
        from core import install as install_mod
        fake_result = install_mod.InstallResult(
            uid=42, name="demo", slug="demo-42",
            target_repo=Path("/x"), apiary_repo=Path("/apiary"),
            state_dir=Path("/apiary/.repos/demo-42"),
            apiary_version="0.1.0", is_first_install=True,
        )
        with mock.patch.object(install_mod, "install", return_value=fake_result):
            msg = cli._per_repo_bootstrap(Path("/x"))
        self.assertIn("uid=42", msg)
        self.assertIn("demo-42", msg)


if __name__ == "__main__":
    unittest.main()
