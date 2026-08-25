"""Tests for incubator/cli.py.

Validation and helper logic are exercised directly with TemporaryDirectory.
The scribe interaction is monkey-patched so tests don't touch real apiary state.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from incubator import cli
from core import install as install_mod
# Reuse the throwaway-apiary fixture from the install tests so the end-to-end
# spawn test never touches the real registry under <apiary>/.repos.
from core.test_install import _make_fake_apiary, _git_init
from scripts import install_git_hooks


def _minimal_apiary(root: Path) -> Path:
    """An apiary root with just a registry — enough for the post-spawn checks.

    Cheaper than _make_fake_apiary's copytree, and sufficient wherever
    core_install.install is mocked: the only thing cli.py reads from
    APIARY_REPO during verification is .repos/registry.json.
    """
    apiary = root / "apiary_min"
    (apiary / ".repos").mkdir(parents=True, exist_ok=True)
    (apiary / ".repos" / "registry.json").write_text("{}", encoding="utf-8")
    return apiary


def _fake_install(apiary: Path):
    """A stand-in for core_install.install that also leaves behind what a real
    one does — the launcher, and a registry entry for the target.

    The old return_value version touched no filesystem, so the fixture
    described a repo that could not exist: no launcher, nothing registered.
    Harmless while nothing looked, and immediately wrong once spawn began
    verifying its own output (#T-2026-254). Note it writes NO local .apiary/
    dir: spawned repos deliberately carry none (55ae7ba), and registration
    lives in main-apiary's registry instead.
    """

    def _install(target, apiary_repo=None, **kwargs):
        target = Path(target)
        launcher = target / ".claude" / "apiary" / "launch.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("# stub launcher\n", encoding="utf-8")

        result = _fake_install_result(target)
        reg_path = Path(apiary) / ".repos" / "registry.json"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            registry = json.loads(reg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            registry = {}
        registry[str(result.uid)] = {
            "name": result.name,
            "real_path": str(Path(target).resolve()),
        }
        reg_path.write_text(json.dumps(registry), encoding="utf-8")
        return result

    return _install


def _fake_install_result(target: Path) -> install_mod.InstallResult:
    return install_mod.InstallResult(
        uid=42, name=target.name, slug=f"{target.name}-42",
        target_repo=target, apiary_repo=Path("/apiary"),
        state_dir=Path("/apiary/.repos") / f"{target.name}-42",
        apiary_version="0.1.0", is_first_install=True,
    )


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

            apiary = _minimal_apiary(Path(td))
            with mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)), \
                 mock.patch.object(cli, "_run_scribe", side_effect=[fake_add, fake_done]), \
                 mock.patch.object(cli, "APIARY_REPO", apiary), \
                 mock.patch.object(cli.core_install, "install",
                                   side_effect=_fake_install(apiary)):
                rc = cli.cmd_spawn(args)

            self.assertEqual(rc, cli.EXIT_OK, msg="spawn should succeed")
            self.assertTrue((target / ".git").is_dir(), "git init should produce .git/")
            self.assertTrue((target / ".gitignore").is_file())
            self.assertTrue((target / "pyproject.toml").is_file())
            self.assertTrue((target / "CLAUDE.md").is_file())
            # No local .apiary/ dir is created — per-repo state is centralized
            # under the main apiary's .repos/<name>-<id>/ store (resolved by the
            # launcher), so the spawned repo holds no apiary state dir.
            self.assertFalse((target / ".apiary").exists())

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
            apiary = _minimal_apiary(Path(td))
            with mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)), \
                 mock.patch.object(cli, "APIARY_REPO", apiary), \
                 mock.patch.object(cli.core_install, "install",
                                   side_effect=_fake_install(apiary)), \
                 mock.patch.object(cli, "_run_scribe", return_value=fake_add_fail), \
                 mock.patch.object(sys, "stderr", new_callable=io.StringIO):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_MIGRATION_FAILED)
            self.assertTrue(target.exists(), "repo should be left intact for manual recovery")
            self.assertTrue((target / ".git").is_dir())

    def test_spawn_bootstrap_failure_aborts_before_migration(self):
        """If per-repo install fails, migration must NOT run and the repo is
        left intact for recovery (EXIT_MIGRATION_FAILED)."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "bootstrap-fail"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            migrate = mock.Mock()
            with mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)), \
                 mock.patch.object(cli.core_install, "install",
                                   side_effect=install_mod.InstallError("no go")), \
                 mock.patch.object(cli, "_migrate_spec", migrate), \
                 mock.patch.object(sys, "stderr", new_callable=io.StringIO):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_MIGRATION_FAILED)
            migrate.assert_not_called()
            self.assertTrue(target.exists(), "repo left intact for manual recovery")


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


class CoreImportTests(unittest.TestCase):
    """The sys.path fix at the top of cli.py must make ``from core import
    install`` resolvable in cli's own process (Bug 2 regression guard)."""

    def test_cli_exposes_core_install(self):
        self.assertTrue(hasattr(cli, "core_install"))
        self.assertTrue(hasattr(cli.core_install, "install"))

    def test_launcher_constant_points_at_per_repo_launcher(self):
        # Bug 1 regression: must be <apiary>/.claude/apiary/launch.py, never
        # the long-gone ~/.claude/apiary_launch.py.
        self.assertEqual(cli.LAUNCHER, cli.APIARY_REPO / ".claude" / "apiary" / "launch.py")


class SpawnEndToEndTests(unittest.TestCase):
    """Hermetic end-to-end spawn against a throwaway fake apiary: real install
    + real scribe subprocesses. Proves the spec lands in the NEW repo's store
    (not apiary's) and the original is closed in apiary — the core regression
    for the bootstrap/migration-order bugs.

    Spec content is kept ASCII to avoid the launcher's PYTHONUTF8 dependency.
    """

    SPEC = (
        "## Goal\n"
        "- **Problem:** Bootstrapping is repetitive.\n"
        "- **Solution:** Scaffold new repos in one shot.\n"
        "- **Value:** Faster starts.\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.apiary = _make_fake_apiary(self.root)
        _git_init(self.apiary)
        # Self-install so apiary has its own launcher + scribe state dir.
        install_mod.install(self.apiary, apiary_repo=self.apiary)
        self.launcher = self.apiary / ".claude" / "apiary" / "launch.py"
        for attr, val in (("APIARY_REPO", self.apiary), ("LAUNCHER", self.launcher)):
            p = mock.patch.object(cli, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def _scribe(self, args, cwd, launcher=None):
        return cli._run_scribe(args, cwd=cwd, launcher=launcher)

    def test_spec_lands_in_new_repo_and_original_closed(self):
        # Seed the spec into apiary's own store.
        seeded = self._scribe(
            ["add", "--type", "context", "--content", self.SPEC, "--session-id", "seed"],
            cwd=self.apiary,
        )
        self.assertEqual(seeded.returncode, 0, seeded.stderr or seeded.stdout)
        m = re.search(r"C-\d{4}-\d+", seeded.stdout)
        self.assertIsNotNone(m, f"could not parse note id from: {seeded.stdout!r}")
        note_id = m.group(0)

        target = self.root / "spawned-proj"
        args = argparse.Namespace(
            path=str(target), spec_note_id=note_id,
            author="x <x@example.com>", session_id="sess",
        )
        with mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            rc = cli.cmd_spawn(args)
        self.assertEqual(rc, cli.EXIT_OK)

        # Bootstrap ran: the new repo has its own launcher.
        new_launcher = target / ".claude" / "apiary" / "launch.py"
        self.assertTrue(new_launcher.is_file(), "per-repo install should create launcher")

        # Spec present in the NEW repo's store.
        new_list = self._scribe(
            ["list", "--type", "context"], cwd=target, launcher=new_launcher
        )
        self.assertEqual(new_list.returncode, 0, new_list.stderr or new_list.stdout)
        self.assertRegex(new_list.stdout, r"C-\d{4}-\d+",
                         "migrated spec should be active in the new repo")

        # Spec ABSENT from apiary's active store (original was closed).
        apiary_list = self._scribe(["list", "--type", "context"], cwd=self.apiary)
        self.assertNotIn(note_id, apiary_list.stdout,
                         "migrated spec must not remain active in apiary")

        # Original is marked done in apiary.
        got = self._scribe(["get", note_id], cwd=self.apiary)
        self.assertIn("done", got.stdout.lower())


if __name__ == "__main__":
    unittest.main()


class VerifySpawnTests(unittest.TestCase):
    """#T-2026-254 — assert a spawn happened instead of trusting that it did.

    The failure this guards against was not a code bug: the CLI worked, but a
    session paraphrased the skill instead of running it, hand-authored the
    files the skill describes, and reported success. Nothing noticed for a
    month. These pin the shape of that repo as detectably incomplete.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.target = self.root / "proj"
        self.target.mkdir()
        self.apiary = _minimal_apiary(self.root)

    def _labels(self, passed: bool):
        checks = cli.verify_spawn(self.target)
        return {label for label, ok, _ in checks if ok is passed}

    def _complete_spawn(self):
        """Lay down everything a real spawn leaves behind."""
        _git_init(self.target)
        launcher = self.target / ".claude" / "apiary" / "launch.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("# stub\n", encoding="utf-8")
        for name in ("pyproject.toml", "CLAUDE.md", ".gitignore"):
            (self.target / name).write_text("x\n", encoding="utf-8")
        reg = self.apiary / ".repos" / "registry.json"
        reg.write_text(
            json.dumps({"7": {"name": "proj", "real_path": str(self.target.resolve())}}),
            encoding="utf-8",
        )
        install_git_hooks.install(self.target)

    def test_hand_authored_directory_fails_the_checks(self):
        # The Hexworld Rebuilt shape: docs written by hand, CLI never run.
        (self.target / "README.md").write_text("# proj\n", encoding="utf-8")
        (self.target / "CLAUDE.md").write_text("# ctx\n", encoding="utf-8")
        with mock.patch.object(cli, "APIARY_REPO", self.apiary):
            failed = self._labels(passed=False)
        self.assertIn("git repo", failed)
        self.assertIn("apiary launcher", failed)
        self.assertIn("registered with apiary", failed)

    def test_complete_spawn_passes_every_check(self):
        self._complete_spawn()
        with mock.patch.object(cli, "APIARY_REPO", self.apiary):
            checks = cli.verify_spawn(self.target)
        failed = [(label, detail) for label, ok, detail in checks if not ok]
        self.assertEqual(failed, [], f"unexpected failures: {failed}")

    def test_secret_scan_hook_is_part_of_the_contract(self):
        # Closes the acceptance criterion left open in #T-2026-253: the
        # incubator claims it installs this hook, and now that claim is checked.
        self._complete_spawn()
        install_git_hooks.main(["--repo", str(self.target), "--uninstall"])
        with mock.patch.object(cli, "APIARY_REPO", self.apiary):
            failed = self._labels(passed=False)
        self.assertIn("secret-scan pre-commit hook", failed)

    def test_unregistered_repo_is_detected(self):
        self._complete_spawn()
        (self.apiary / ".repos" / "registry.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(cli, "APIARY_REPO", self.apiary):
            failed = self._labels(passed=False)
        self.assertIn("registered with apiary", failed)

    def test_unreadable_registry_reports_rather_than_raising(self):
        self._complete_spawn()
        (self.apiary / ".repos" / "registry.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(cli, "APIARY_REPO", self.apiary):
            checks = cli.verify_spawn(self.target)
        detail = next(d for label, _, d in checks if label == "registered with apiary")
        self.assertIn("registry unreadable", detail)


class VerifyCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_missing_directory_is_a_validation_error(self):
        args = argparse.Namespace(path=str(self.root / "nope"))
        self.assertEqual(cli.cmd_verify(args), cli.EXIT_VALIDATION)

    def test_incomplete_target_exits_verify_failed(self):
        target = self.root / "proj"
        target.mkdir()
        args = argparse.Namespace(path=str(target))
        with mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            rc = cli.cmd_verify(args)
        self.assertEqual(rc, cli.EXIT_VERIFY_FAILED)

    def test_verify_is_a_registered_subcommand(self):
        # Guards the skill's Step 5 invocation from silently 404-ing.
        self.assertIn("verify", cli.COMMANDS)

