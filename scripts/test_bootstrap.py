#!/usr/bin/env python3
"""Tests for scripts/bootstrap.py.

Covers the post-C-2026-46 centralized seed:
  - fresh-clone seed creates the scribe typed-year layout under the
    resolver-allocated state dir (<apiary>/.repos/<name>-<id>/scribe/)
  - idempotent re-run is a no-op on all paths
  - legacy-state guard refuses to seed when ~/.claude/projects/<key>/
    still holds unmigrated scribe state
  - auto-startup flag creation is gated on absence
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import bootstrap  # noqa: E402


class BootstrapTestBase(unittest.TestCase):
    """Common scaffolding: isolate every filesystem touch under a tmpdir.

    Patches the module-level CLAUDE_DIR, PROJECTS_DIR, and AUTO_STARTUP_FLAG
    constants so tests never read or write the user's real ~/.claude.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name).resolve()
        self.fake_home = self.tmp_path / "home" / ".claude"
        self.fake_projects = self.fake_home / "projects"
        self.fake_auto_flag = self.fake_home / "auto-startup-enabled"
        self.fake_repo = self.tmp_path / "apiary-repo"
        self.fake_repo.mkdir()
        # The state resolver requires the repo to be a git repo.
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=str(self.fake_repo),
            check=True,
            capture_output=True,
        )

        self._patches = [
            mock.patch.object(bootstrap, "CLAUDE_DIR", self.fake_home),
            mock.patch.object(bootstrap, "PROJECTS_DIR", self.fake_projects),
            mock.patch.object(bootstrap, "AUTO_STARTUP_FLAG", self.fake_auto_flag),
            # Block the requirements check: it imports real packages and
            # emits warnings in environments where they're missing.
            mock.patch.object(bootstrap, "_check_requirements", lambda result: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    @property
    def scribe_dir(self) -> Path:
        """Resolve the scribe dir via the registry (auto-registers if needed)."""
        return bootstrap._resolve_scribe_dir(self.fake_repo)


class FreshSeedTests(BootstrapTestBase):
    def test_creates_centralized_scribe_dir(self):
        result = bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(result.warnings, [])
        self.assertTrue(self.scribe_dir.is_dir())
        # State landed under <repo>/.repos/<name>-<id>/scribe/ (the
        # resolver uses self.fake_repo as the apiary root in tests).
        self.assertIn(".repos", self.scribe_dir.parts)
        # A pointer breadcrumb was written back into the repo.
        self.assertTrue((self.fake_repo / ".apiary" / "pointer").is_file())

    def test_creates_typed_year_layout(self):
        bootstrap.bootstrap(self.fake_repo)
        # Every type folder and the learnings folder must exist
        for folder in ("todos", "handoffs", "decisions", "wishlists",
                       "blockers", "references", "context", "general",
                       "learnings"):
            self.assertTrue(
                (self.scribe_dir / folder).is_dir(),
                f"missing type folder: {folder}",
            )
        # Flat jsonl files must NOT be seeded (pre-v2 layout is gone)
        self.assertFalse((self.scribe_dir / "notes.jsonl").exists())
        self.assertFalse((self.scribe_dir / "learnings.jsonl").exists())

    def test_creates_memory_dir_and_index(self):
        bootstrap.bootstrap(self.fake_repo)
        memory_dir = self.scribe_dir / "memory"
        self.assertTrue(memory_dir.is_dir())
        self.assertTrue((memory_dir / "MEMORY.md").is_file())

    def test_creates_auto_startup_flag(self):
        bootstrap.bootstrap(self.fake_repo)
        self.assertTrue(self.fake_auto_flag.is_file())
        self.assertEqual(
            self.fake_auto_flag.read_text(encoding="utf-8"), "enabled"
        )

    def test_does_not_create_legacy_projects_dir(self):
        """Fresh seed must NOT create ~/.claude/projects/<key>/ anymore."""
        bootstrap.bootstrap(self.fake_repo)
        # PROJECTS_DIR itself may or may not exist — bootstrap no longer
        # creates it. What matters is that no scribe files were seeded there.
        legacy_scribe_candidates = [
            self.fake_projects / "claude-apiary" / "notes.jsonl",
            self.fake_projects / "claude-apiary" / "learnings.jsonl",
            self.fake_projects / "claude-apiary" / "memory",
        ]
        for p in legacy_scribe_candidates:
            self.assertFalse(
                p.exists(), f"unexpected legacy seed at {p}"
            )


class IdempotencyTests(BootstrapTestBase):
    def test_rerun_creates_nothing_new(self):
        first = bootstrap.bootstrap(self.fake_repo)
        self.assertTrue(first.created)

        second = bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(second.created, [])
        self.assertTrue(second.existed)
        self.assertEqual(second.warnings, [])

    def test_rerun_does_not_clobber_preexisting_files(self):
        bootstrap.bootstrap(self.fake_repo)
        todos_index = self.scribe_dir / "todos" / "index.jsonl"
        self.assertTrue(todos_index.parent.is_dir())
        todos_index.write_text('{"display_id":"T-2026-1"}\n', encoding="utf-8")

        bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(
            todos_index.read_text(encoding="utf-8"),
            '{"display_id":"T-2026-1"}\n',
        )



class LegacyStateGuardTests(BootstrapTestBase):
    """Bootstrap must refuse to seed when unmigrated legacy state exists."""

    def _make_legacy_state(self, project_key: str):
        legacy_dir = self.fake_projects / project_key
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "notes.jsonl").write_text(
            '{"id":1,"type":"todo","content":"legacy"}\n', encoding="utf-8"
        )
        return legacy_dir

    def test_refuses_when_stable_key_has_legacy_state(self):
        legacy_dir = self._make_legacy_state("claude-apiary")
        # Mock project_key resolution to match the stable key we just seeded
        with mock.patch.object(
            bootstrap, "get_project_key", return_value="claude-apiary"
        ):
            result = bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn(str(legacy_dir), result.warnings[0])
        self.assertIn("migrate_scribe_state.py", result.warnings[0])
        self.assertFalse(
            (self.scribe_dir / "todos").exists(),
            "bootstrap laid out typed-year folders despite legacy state being present",
        )

    def test_refuses_when_cwd_key_has_legacy_state(self):
        """Detects state under both the stable key AND the cwd-derived key."""
        legacy_key = bootstrap.project_key_from_path(self.fake_repo)
        legacy_dir = self._make_legacy_state(legacy_key)
        with mock.patch.object(
            bootstrap, "get_project_key", return_value="claude-apiary"
        ):
            result = bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn(str(legacy_dir), result.warnings[0])

    def test_does_not_refuse_when_scribe_dir_already_populated(self):
        """If in-repo state already exists, the legacy guard must not fire."""
        self._make_legacy_state("claude-apiary")
        (self.scribe_dir / "todos").mkdir(parents=True)

        with mock.patch.object(
            bootstrap, "get_project_key", return_value="claude-apiary"
        ):
            result = bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(result.warnings, [])

    def test_does_not_refuse_when_no_legacy_state(self):
        with mock.patch.object(
            bootstrap, "get_project_key", return_value="claude-apiary"
        ):
            result = bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(result.warnings, [])
        self.assertTrue((self.scribe_dir / "todos").is_dir())


class AutoStartupFlagTests(BootstrapTestBase):
    def test_preexisting_flag_not_overwritten(self):
        self.fake_home.mkdir(parents=True)
        self.fake_auto_flag.write_text("preexisting", encoding="utf-8")
        bootstrap.bootstrap(self.fake_repo)
        self.assertEqual(
            self.fake_auto_flag.read_text(encoding="utf-8"), "preexisting"
        )


class CronHealthHookTests(unittest.TestCase):
    """The tail-end cron_health check must never affect bootstrap's exit."""

    def test_cron_health_failure_is_swallowed(self):
        from runner import cron_health

        with mock.patch.object(
            cron_health, "run_bootstrap_check", side_effect=RuntimeError("boom")
        ):
            # Should not raise — bootstrap's exit must be independent.
            bootstrap._run_cron_health_check(quiet=True)

    def test_cron_health_invoked_when_available(self):
        from runner import cron_health

        called = []

        def fake_check(*, stream):
            called.append(True)

        with mock.patch.object(
            cron_health, "run_bootstrap_check", side_effect=fake_check
        ):
            bootstrap._run_cron_health_check(quiet=True)
        self.assertEqual(called, [True])


if __name__ == "__main__":
    unittest.main()
