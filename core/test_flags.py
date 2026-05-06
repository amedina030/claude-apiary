"""Tests for ``core/flags.py`` per-repo + global flag handling."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import flags


class FlagsResolutionTests(unittest.TestCase):
    """The resolution order is: per-repo (if bootstrapped repo in scope)
    -> global. The per-repo path is selected via $CLAUDE_PROJECT_DIR or
    $APIARY_TARGET_REPO; missing both, the global path is used."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "myrepo"
        (self.repo / ".claude" / "apiary").mkdir(parents=True)
        # Save and clear env so individual tests can set them
        self._saved_env = {}
        for var in ("CLAUDE_PROJECT_DIR", "APIARY_TARGET_REPO"):
            self._saved_env[var] = os.environ.pop(var, None)
        # Redirect the global CLAUDE_DIR to a tmpdir so we don't touch
        # the user's real ~/.claude.
        self._saved_claude_dir = flags.CLAUDE_DIR
        flags.CLAUDE_DIR = self.root / "fake-home" / ".claude"
        flags.CLAUDE_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        flags.CLAUDE_DIR = self._saved_claude_dir
        for var, val in self._saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def test_per_repo_flag_wins_when_present(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        (self.repo / ".claude" / "apiary" / "flags").mkdir(parents=True)
        (self.repo / ".claude" / "apiary" / "flags" / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        # Global is unset
        self.assertTrue(flags.is_enabled("budgeter-log"))

    def test_per_repo_off_blocks_global_fallback(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        # Per-repo flag NOT set, but bootstrapped repo is in scope.
        # Global is set.
        (flags.CLAUDE_DIR / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        # Per-repo "off" should win — phase-1 contract per docstring.
        self.assertFalse(flags.is_enabled("budgeter-log"))

    def test_global_flag_works_when_no_repo_in_scope(self):
        # No env vars; cwd's git root may or may not resolve. If it does,
        # it would be the repo of THIS test process — we don't want that.
        # Force "no repo" by setting CLAUDE_PROJECT_DIR to a non-bootstrapped dir.
        plain = self.root / "plain"
        plain.mkdir()
        os.environ["APIARY_TARGET_REPO"] = str(plain)
        (flags.CLAUDE_DIR / "budgeter-log-enabled").write_text("on", encoding="utf-8")
        # plain has no .claude/apiary/, so per-repo dir doesn't exist —
        # falls through to global.
        self.assertTrue(flags.is_enabled("budgeter-log"))

    def test_enable_writes_per_repo_when_repo_bootstrapped(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        flags.enable("test-flag")
        per_repo = self.repo / ".claude" / "apiary" / "flags" / "test-flag-enabled"
        self.assertTrue(per_repo.is_file())
        self.assertFalse((flags.CLAUDE_DIR / "test-flag-enabled").exists())

    def test_disable_only_removes_per_repo_file(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        # Both files exist
        per_repo = self.repo / ".claude" / "apiary" / "flags"
        per_repo.mkdir(parents=True)
        (per_repo / "test-flag-enabled").write_text("x", encoding="utf-8")
        (flags.CLAUDE_DIR / "test-flag-enabled").write_text("x", encoding="utf-8")

        flags.disable("test-flag")
        self.assertFalse((per_repo / "test-flag-enabled").exists())
        # Global file is preserved (caller can clean it up separately)
        self.assertTrue((flags.CLAUDE_DIR / "test-flag-enabled").exists())

    def test_toggle_round_trip(self):
        os.environ["APIARY_TARGET_REPO"] = str(self.repo)
        self.assertFalse(flags.is_enabled("toggleme"))
        self.assertTrue(flags.toggle("toggleme"))
        self.assertTrue(flags.is_enabled("toggleme"))
        self.assertFalse(flags.toggle("toggleme"))
        self.assertFalse(flags.is_enabled("toggleme"))


if __name__ == "__main__":
    unittest.main()
