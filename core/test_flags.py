"""Tests for ``core/flags.py`` per-repo flag handling."""
from __future__ import annotations

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
        self.assertTrue((self.repo / ".claude" / "apiary" / "flags" / "test-flag-enabled").is_file())

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


if __name__ == "__main__":
    unittest.main()
