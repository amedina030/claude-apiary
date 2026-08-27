"""Tests for ``core/flags.py`` per-repo flag handling and its CLI."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import flags


class FlagsResolutionTests(unittest.TestCase):
    """Per-repo path is selected via ``$CLAUDE_PROJECT_DIR`` /
    ``$APIARY_TARGET_REPO`` / cwd's git root. With none of those set,
    ``is_enabled`` returns False (no global fallback post-migration)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "myrepo"
        (self.repo / ".claude" / "apiary").mkdir(parents=True)
        # Save / clear env so individual tests can set as needed
        self._saved_env = {}
        for var in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
            self._saved_env[var] = os.environ.pop(var, None)

    def tearDown(self) -> None:
        for var, val in self._saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def test_is_enabled_true_when_per_repo_flag_present(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        flag_dir = self.repo / ".claude" / "apiary" / "flags"
        flag_dir.mkdir(parents=True)
        (flag_dir / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        self.assertTrue(flags.is_enabled("budgeter-log"))

    def test_is_enabled_false_when_per_repo_flag_absent(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        self.assertFalse(flags.is_enabled("budgeter-log"))

    def test_is_enabled_false_when_no_repo_resolvable(self):
        # No env, and we don't want to actually run `git rev-parse` (cwd
        # is unpredictable in pytest); set APIARY_TARGET_REPO to a non-
        # directory to short-circuit env resolution and force the cwd
        # branch. Since cwd is the repo running tests from, we can't
        # reliably assert "no repo" — instead test that the error is
        # swallowed and the call returns False even without a flag dir.
        nonexistent = self.root / "nope"
        os.environ["APIARY_TARGET_REPO"] = str(nonexistent)
        # Resolution falls through to git rev-parse on cwd. As long as
        # no flag file is present (we never wrote one for `bogus-flag`),
        # is_enabled is False.
        self.assertFalse(flags.is_enabled("bogus-flag-name-that-isnt-set-anywhere"))

    def test_enable_writes_per_repo_file(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        flags.enable("test-flag")
        self.assertTrue(
            (self.repo / ".claude" / "apiary" / "flags" / "test-flag-enabled").is_file()
        )

    def test_disable_is_idempotent(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        flags.disable("missing-flag")  # no-op when nothing to remove
        flags.enable("test-flag")
        flags.disable("test-flag")
        flags.disable("test-flag")  # second call is also a no-op
        self.assertFalse(flags.is_enabled("test-flag"))

    def test_toggle_round_trip(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        self.assertFalse(flags.is_enabled("toggleme"))
        self.assertTrue(flags.toggle("toggleme"))
        self.assertTrue(flags.is_enabled("toggleme"))
        self.assertFalse(flags.toggle("toggleme"))
        self.assertFalse(flags.is_enabled("toggleme"))


class FlagsRepoUnresolvedTests(unittest.TestCase):
    """``enable`` raises when no repo is in scope so misconfigured
    callers fail loudly. ``is_enabled`` and ``disable`` swallow the
    error (graceful no-ops) so hooks running in odd environments
    don't crash."""

    def setUp(self) -> None:
        self._saved_env = {}
        for var in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
            self._saved_env[var] = os.environ.pop(var, None)
        # Force "no repo" by setting APIARY_TARGET_REPO to a nonexistent path.
        os.environ["APIARY_TARGET_REPO"] = "/this/path/definitely/does/not/exist/12345"

    def tearDown(self) -> None:
        for var, val in self._saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def test_enable_raises_when_unresolved(self):
        # Run from a directory that's NOT a git repo so the cwd fallback
        # also fails. Use a tmpdir.
        with tempfile.TemporaryDirectory() as td:
            saved_cwd = os.getcwd()
            os.chdir(td)
            try:
                with self.assertRaises(flags.FlagsRepoUnresolved):
                    flags.enable("any-flag")
            finally:
                os.chdir(saved_cwd)


class FlagsCliTests(unittest.TestCase):
    """``flags.main(argv)`` — the entry point ``/budgeter`` shells out to via
    the launcher. Each verb prints ``ON``/``OFF`` and returns 0; an
    unresolvable repo or a malformed name returns 1."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "myrepo"
        (self.repo / ".claude" / "apiary").mkdir(parents=True)
        # ``_per_repo_root`` reads CLAUDE_PROJECT_DIR *before*
        # APIARY_TARGET_REPO, so clear it or a live session's value wins.
        self._saved_env = {}
        for var in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
            self._saved_env[var] = os.environ.pop(var, None)
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)

    def tearDown(self) -> None:
        for var, val in self._saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Call ``flags.main(argv)``, returning (exit code, stripped stdout)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = flags.main(argv)
        return code, buf.getvalue().strip()

    def _flag_file(self, name: str) -> Path:
        return self.repo / ".claude" / "apiary" / "flags" / f"{name}-enabled"

    def test_enable_prints_on_and_creates_file(self):
        code, out = self._run(["enable", "budgeter-log"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "ON")
        self.assertTrue(self._flag_file("budgeter-log").is_file())

    def test_enable_is_idempotent(self):
        self._run(["enable", "budgeter-log"])
        code, out = self._run(["enable", "budgeter-log"])
        self.assertEqual((code, out), (0, "ON"))
        self.assertTrue(flags.is_enabled("budgeter-log"))

    def test_disable_prints_off_and_removes_file(self):
        flags.enable("auto-startup")
        code, out = self._run(["disable", "auto-startup"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "OFF")
        self.assertFalse(self._flag_file("auto-startup").exists())

    def test_disable_is_idempotent(self):
        code, out = self._run(["disable", "never-set"])
        self.assertEqual((code, out), (0, "OFF"))

    def test_toggle_round_trip(self):
        code, out = self._run(["toggle", "budgeter-session-warn"])
        self.assertEqual((code, out), (0, "ON"))
        self.assertTrue(flags.is_enabled("budgeter-session-warn"))
        code, out = self._run(["toggle", "budgeter-session-warn"])
        self.assertEqual((code, out), (0, "OFF"))
        self.assertFalse(flags.is_enabled("budgeter-session-warn"))

    def test_status_reports_state_without_changing_it(self):
        code, out = self._run(["status", "budgeter-log"])
        self.assertEqual((code, out), (0, "OFF"))
        self.assertFalse(self._flag_file("budgeter-log").exists())

        flags.enable("budgeter-log")
        code, out = self._run(["status", "budgeter-log"])
        self.assertEqual((code, out), (0, "ON"))
        self.assertTrue(self._flag_file("budgeter-log").is_file())

    def test_toggle_writes_where_is_enabled_reads(self):
        """The whole point of B4: the CLI and the hooks must agree on the path."""
        self._run(["toggle", "budgeter-log"])
        self.assertTrue(flags.is_enabled("budgeter-log"))
        self.assertEqual(self._flag_file("budgeter-log"), flags._flag_path("budgeter-log"))

    def test_malformed_name_exits_1_and_writes_nothing(self):
        # NB: nothing starting with "-" — argparse would reject that as an
        # unknown option (exit 2) before the name check ever runs.
        for bad in ("../escape", "with/slash", "with\\backslash", "has space", ".."):
            with self.subTest(name=bad):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                    code = flags.main(["enable", bad])
                self.assertEqual(code, 1)
                self.assertEqual(buf.getvalue().strip(), "")
        flags_dir = self.repo / ".claude" / "apiary" / "flags"
        self.assertFalse(flags_dir.exists(), "a rejected name must not create the flags dir")

    def test_unknown_verb_is_an_argparse_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                flags.main(["frobnicate", "budgeter-log"])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_verb_is_an_argparse_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                flags.main([])
        self.assertEqual(ctx.exception.code, 2)


class FlagsCliUnresolvedTests(unittest.TestCase):
    """Every verb exits 1 when no bootstrapped repo is in scope — including
    ``disable``/``status``, whose library forms swallow the error."""

    def setUp(self) -> None:
        self._saved_env = {}
        for var in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
            self._saved_env[var] = os.environ.pop(var, None)
        os.environ["APIARY_TARGET_REPO"] = "/this/path/definitely/does/not/exist/12345"
        # cwd must not be a git tree either, or the git-root fallback resolves.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._saved_cwd)
        for var, val in self._saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def test_every_verb_exits_1_with_reason_on_stderr(self):
        for verb in ("toggle", "enable", "disable", "status"):
            with self.subTest(verb=verb):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = flags.main([verb, "budgeter-log"])
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue().strip(), "")
                self.assertIn("cannot resolve a bootstrapped repo", err.getvalue())


if __name__ == "__main__":
    unittest.main()
