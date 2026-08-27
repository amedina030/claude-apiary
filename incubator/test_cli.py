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
from core import install as install_mod

# Reuse the throwaway-apiary fixture from the install tests so the end-to-end
# spawn test never touches the real registry under <apiary>/.repos.
from core import testing
from core.test_install import _git_init
from incubator import cli
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
        uid=42,
        name=target.name,
        slug=f"{target.name}-42",
        target_repo=target,
        apiary_repo=Path("/apiary"),
        state_dir=Path("/apiary/.repos") / f"{target.name}-42",
        apiary_version="0.1.0",
        is_first_install=True,
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
            existing = Path(td).resolve() / "already-here"
            existing.mkdir()
            path, err = cli._validate_target(str(existing))
            self.assertIsNone(path)
            self.assertIn("already exists", err)

    def test_missing_parent_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td).resolve() / "no-such-parent" / "child"
            path, err = cli._validate_target(str(target))
            self.assertIsNone(path)
            self.assertIn("parent directory", err)

    def test_inside_existing_git_repo_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            outer = Path(td).resolve()
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
            target = Path(td).resolve() / "fresh-project"
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
            target = Path(td).resolve() / "spawn-target"

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

            apiary = _minimal_apiary(Path(td).resolve())
            with (
                mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)),
                mock.patch.object(cli, "_run_scribe", side_effect=[fake_add, fake_done]),
                mock.patch.object(cli, "APIARY_REPO", apiary),
                mock.patch.object(cli.core_install, "install", side_effect=_fake_install(apiary)),
            ):
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
            target = Path(td).resolve() / "rollback-target"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            with (
                mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)),
                mock.patch.object(cli, "_run_git_init", return_value=(False, "boom")),
                mock.patch.object(sys, "stderr", new_callable=io.StringIO),
            ):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_SPAWN_FAILED)
            self.assertFalse(target.exists(), "partial directory should be removed")

    def test_spawn_reports_partial_on_migration_failure(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td).resolve() / "partial-target"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            fake_add_fail = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="scribe blew up"
            )
            apiary = _minimal_apiary(Path(td).resolve())
            with (
                mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)),
                mock.patch.object(cli, "APIARY_REPO", apiary),
                mock.patch.object(cli.core_install, "install", side_effect=_fake_install(apiary)),
                mock.patch.object(cli, "_run_scribe", return_value=fake_add_fail),
                mock.patch.object(sys, "stderr", new_callable=io.StringIO),
            ):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_MIGRATION_FAILED)
            self.assertTrue(target.exists(), "repo should be left intact for manual recovery")
            self.assertTrue((target / ".git").is_dir())

    def test_spawn_bootstrap_failure_aborts_before_migration(self):
        """If per-repo install fails, migration must NOT run and the repo is
        left intact for recovery (EXIT_MIGRATION_FAILED)."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td).resolve() / "bootstrap-fail"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            migrate = mock.Mock()
            with (
                mock.patch.object(cli, "_fetch_spec", return_value=(self.SAMPLE_SPEC, None)),
                mock.patch.object(
                    cli.core_install, "install", side_effect=install_mod.InstallError("no go")
                ),
                mock.patch.object(cli, "_migrate_spec", migrate),
                mock.patch.object(sys, "stderr", new_callable=io.StringIO),
            ):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_MIGRATION_FAILED)
            migrate.assert_not_called()
            self.assertTrue(target.exists(), "repo left intact for manual recovery")


class MigrateSpecTests(unittest.TestCase):
    """B9/B10: the spec must not travel on argv, and a half-done migration must
    not tell the operator to re-run the step that already succeeded."""

    SPEC = "# Spec\n" + ("filler line to make this realistically long\n" * 1200)

    def test_spec_travels_by_file_not_argv(self):
        """A /refine spec routinely runs several KB; Windows CreateProcess caps
        a command line at 32,767 chars."""
        self.assertGreater(len(self.SPEC), 32767, "fixture must exceed the argv cap")
        seen: dict = {}

        def fake_run_scribe(args, cwd, launcher=None):
            if args and args[0] == "add":
                seen["args"] = list(args)
                idx = args.index("--content-file")
                seen["path"] = Path(args[idx + 1])
                seen["body"] = seen["path"].read_text(encoding="utf-8")
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Added C-2026-1 (context)\n", stderr=""
            )

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cli, "_run_scribe", side_effect=fake_run_scribe):
                ok, _msg, added = cli._migrate_spec(Path(td).resolve() / "t", self.SPEC, "C-2026-999", "sess")

        self.assertTrue(ok)
        self.assertTrue(added)
        self.assertIn("--content-file", seen["args"])
        self.assertNotIn("--content", seen["args"], "the spec body must never be an argv element")
        self.assertEqual(seen["body"], self.SPEC)
        self.assertFalse(seen["path"].exists(), "the staged spec file is temporary")

    def test_oserror_from_the_scribe_spawn_is_reported_not_raised(self):
        """subprocess.run can raise (missing interpreter, argv too long). The
        CLI must return its documented failure, not a traceback."""
        with mock.patch.object(
            cli.subprocess, "run", side_effect=OSError("[WinError 206] filename too long")
        ):
            result = cli._run_scribe(["add"], cwd=Path.cwd())
        self.assertEqual(result.returncode, 1)
        self.assertIn("filename too long", result.stderr)

    def test_migrate_survives_an_oserror(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cli.subprocess, "run", side_effect=OSError("boom")):
                ok, msg, added = cli._migrate_spec(Path(td).resolve() / "t", "spec", "C-2026-999", None)
        self.assertFalse(ok)
        self.assertFalse(added)
        self.assertIn("boom", msg)

    def test_add_failure_reports_the_spec_was_not_migrated(self):
        fail_add = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="scribe blew up"
        )
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cli, "_run_scribe", return_value=fail_add):
                ok, msg, added = cli._migrate_spec(Path(td).resolve() / "t", "spec", "C-2026-999", None)
        self.assertFalse(ok)
        self.assertFalse(added, "add failed, so nothing landed in the new repo")
        self.assertIn("scribe blew up", msg)

    def test_done_failure_still_reports_the_spec_as_added(self):
        ok_add = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Added C-2026-1 (context)\n", stderr=""
        )
        fail_done = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no such note"
        )
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cli, "_run_scribe", side_effect=[ok_add, fail_done]):
                ok, msg, added = cli._migrate_spec(Path(td).resolve() / "t", "spec", "C-2026-999", None)
        self.assertFalse(ok)
        self.assertTrue(added, "the spec did land — only the close failed")
        self.assertIn("no such note", msg)


class RecoveryHintTests(unittest.TestCase):
    """B10: the hint printed on a partial migration must match what is left to do."""

    SPEC = SkeletonLayoutTests.SAMPLE_SPEC

    def _spawn_with(self, scribe_results) -> str:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td).resolve() / "hint-target"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            apiary = _minimal_apiary(Path(td).resolve())
            err = io.StringIO()
            with (
                mock.patch.object(cli, "_fetch_spec", return_value=(self.SPEC, None)),
                mock.patch.object(cli, "APIARY_REPO", apiary),
                mock.patch.object(cli.core_install, "install", side_effect=_fake_install(apiary)),
                mock.patch.object(cli, "_run_scribe", side_effect=scribe_results),
                mock.patch.object(sys, "stderr", err),
            ):
                rc = cli.cmd_spawn(args)
            self.assertEqual(rc, cli.EXIT_MIGRATION_FAILED)
            return err.getvalue()

    def test_hint_after_add_succeeded_says_close_the_original_only(self):
        ok_add = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Added C-2026-1 (context)\n", stderr=""
        )
        fail_done = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no such note"
        )
        err = self._spawn_with([ok_add, fail_done])
        self.assertIn("done C-2026-999", err)
        self.assertIn("ALREADY", err)
        # Re-running `add` would file a second copy of the spec.
        self.assertNotIn("--type context", err)
        self.assertNotIn("--content-file <path>", err)

    def test_hint_after_add_failed_covers_both_steps(self):
        fail_add = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="scribe blew up"
        )
        err = self._spawn_with([fail_add])
        self.assertIn("--content-file", err)
        self.assertNotIn(
            "--content <spec-body>", err, "the recovery command must not put the spec on argv"
        )
        self.assertIn("done C-2026-999", err)


class TemplateTests(unittest.TestCase):
    """Every spawned repo ships these verbatim, so a wrong example is a bug in
    13 repos at once."""

    def test_budgeter_example_uses_a_date_budgeter_can_parse(self):
        from budgeter import report

        text = (cli.TEMPLATES_DIR / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
        values = re.findall(r"report\.py\s+--(?:since|date)\s+(\S+)", text)
        self.assertTrue(values, "template should still show a dated report example")
        for value in values:
            with self.subTest(value=value):
                report.parse_date(value)  # ValueError on '7d'

    def test_budgeter_example_flags_exist(self):
        text = (cli.TEMPLATES_DIR / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
        flags = set(re.findall(r"budgeter/report\.py\s+(--[a-z-]+)", text))
        help_text = subprocess.run(
            [sys.executable, str(cli.APIARY_REPO / "budgeter" / "report.py"), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        for flag in flags:
            with self.subTest(flag=flag):
                self.assertIn(flag, help_text)

    def test_gitignore_has_no_dead_apiary_entries(self):
        """Spawned repos carry no local .apiary/ dir (55ae7ba), so rules for one
        are noise that implies state lives somewhere it does not."""
        text = (cli.TEMPLATES_DIR / "gitignore.tmpl").read_text(encoding="utf-8")
        entries = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            [e for e in entries if ".apiary" in e],
            [],
            "gitignore.tmpl still carries dead .apiary/ rules",
        )

    def test_gitignore_leaves_the_claude_seam_to_the_installer(self):
        """core.install appends the stepwise `.claude/` block only when the file
        has no `.claude` rule yet — the template must not pre-empt it."""
        text = (cli.TEMPLATES_DIR / "gitignore.tmpl").read_text(encoding="utf-8")
        entries = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual([e for e in entries if e in install_mod._GITIGNORE_PRESENT], [])


class SpecFetchErrorTests(unittest.TestCase):
    def test_missing_spec_returns_exit_3(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td).resolve() / "x"
            args = argparse.Namespace(
                path=str(target),
                spec_note_id="C-2026-999",
                author="x",
                session_id=None,
            )
            with (
                mock.patch.object(cli, "_fetch_spec", return_value=(None, "not found")),
                mock.patch.object(sys, "stderr", new_callable=io.StringIO),
            ):
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
        # This one really runs `launch.py scribe/notes.py` out of the fake
        # apiary, so it needs the actual core/ and scribe/ trees there —
        # the default fake carries only what install *reads*.
        self.apiary = testing.make_fake_apiary(
            self.root,
            git=True,
            extra_trees=("core", "scribe"),
        )
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
            path=str(target),
            spec_note_id=note_id,
            author="x <x@example.com>",
            session_id="sess",
        )
        with mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            rc = cli.cmd_spawn(args)
        self.assertEqual(rc, cli.EXIT_OK)

        # Bootstrap ran: the new repo has its own launcher.
        new_launcher = target / ".claude" / "apiary" / "launch.py"
        self.assertTrue(new_launcher.is_file(), "per-repo install should create launcher")

        # Spec present in the NEW repo's store.
        new_list = self._scribe(["list", "--type", "context"], cwd=target, launcher=new_launcher)
        self.assertEqual(new_list.returncode, 0, new_list.stderr or new_list.stdout)
        self.assertRegex(
            new_list.stdout, r"C-\d{4}-\d+", "migrated spec should be active in the new repo"
        )

        # Spec ABSENT from apiary's active store (original was closed).
        apiary_list = self._scribe(["list", "--type", "context"], cwd=self.apiary)
        self.assertNotIn(
            note_id, apiary_list.stdout, "migrated spec must not remain active in apiary"
        )

        # Original is marked done in apiary.
        got = self._scribe(["get", note_id], cwd=self.apiary)
        self.assertIn("done", got.stdout.lower())

    def test_multi_kilobyte_spec_migrates_intact(self):
        """B9 regression: a spec too long for a Windows command line still
        migrates, because it goes to scribe through --content-file."""
        marker_head = "SPEC-HEAD-MARKER"
        marker_tail = "SPEC-TAIL-MARKER"
        big = (
            f"{marker_head}\n"
            + self.SPEC
            + ("a line of spec body that is here only to add length\n" * 900)
            + f"{marker_tail}\n"
        )
        self.assertGreater(len(big), 32767, "fixture must exceed the argv cap")

        seed_file = self.root / "seed-spec.md"
        seed_file.write_text(big, encoding="utf-8")
        seeded = self._scribe(
            ["add", "--type", "context", "--content-file", str(seed_file), "--session-id", "seed"],
            cwd=self.apiary,
        )
        self.assertEqual(seeded.returncode, 0, seeded.stderr or seeded.stdout)
        note_id = re.search(r"C-\d{4}-\d+", seeded.stdout).group(0)

        target = self.root / "big-spec-proj"
        args = argparse.Namespace(
            path=str(target),
            spec_note_id=note_id,
            author="x <x@example.com>",
            session_id="sess",
        )
        with mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            rc = cli.cmd_spawn(args)
        self.assertEqual(rc, cli.EXIT_OK)

        new_launcher = target / ".claude" / "apiary" / "launch.py"
        listed = self._scribe(["list", "--type", "context"], cwd=target, launcher=new_launcher)
        new_id = re.search(r"C-\d{4}-\d+", listed.stdout).group(0)
        got = self._scribe(["get", new_id], cwd=target, launcher=new_launcher)
        self.assertEqual(got.returncode, 0, got.stderr or got.stdout)
        self.assertIn(marker_head, got.stdout, "spec head lost in migration")
        self.assertIn(marker_tail, got.stdout, "spec tail lost in migration")


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
        self.root = Path(self._tmp.name).resolve()
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
        self.root = Path(self._tmp.name).resolve()

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
