#!/usr/bin/env python3
"""Tests for scripts/migrate_scribe_state.py (todo #266)."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import migrate_scribe_state as migrate  # noqa: E402


class MigrateHappyPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.source = self.tmp_path / "legacy"
        self.target = self.tmp_path / "repo" / ".apiary" / "scribe"
        self.source.mkdir()
        # Populate legacy state
        (self.source / "notes.jsonl").write_text(
            '{"id":1,"type":"todo"}\n', encoding="utf-8"
        )
        (self.source / "notes_archive.jsonl").write_text(
            '{"id":100,"type":"handoff"}\n', encoding="utf-8"
        )
        (self.source / "learnings.jsonl").write_text(
            '{"id":1}\n', encoding="utf-8"
        )
        (self.source / "backfill_skip.json").write_text(
            '{"skipped": []}', encoding="utf-8"
        )
        # Lock siblings that must be skipped
        (self.source / "notes.jsonl.lock").write_text("", encoding="utf-8")
        (self.source / "learnings.jsonl.lock").write_text("", encoding="utf-8")
        # memory/ subdir with a file
        (self.source / "memory").mkdir()
        (self.source / "memory" / "user_role.md").write_text(
            "some memory\n", encoding="utf-8"
        )
        (self.source / "memory" / "user_role.md.lock").write_text(
            "", encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_copies_all_state_files(self):
        report = migrate.migrate(self.source, self.target)
        self.assertTrue((self.target / "notes.jsonl").is_file())
        self.assertTrue((self.target / "notes_archive.jsonl").is_file())
        self.assertTrue((self.target / "learnings.jsonl").is_file())
        self.assertTrue((self.target / "backfill_skip.json").is_file())
        for name in migrate.STATE_FILES:
            self.assertEqual(report["files"][name], "copied")

    def test_copies_memory_subdir_recursively(self):
        migrate.migrate(self.source, self.target)
        copied = self.target / "memory" / "user_role.md"
        self.assertTrue(copied.is_file())
        self.assertIn("some memory", copied.read_text(encoding="utf-8"))

    def test_skips_lock_files(self):
        migrate.migrate(self.source, self.target)
        self.assertFalse((self.target / "notes.jsonl.lock").exists())
        self.assertFalse(
            (self.target / "memory" / "user_role.md.lock").exists()
        )

    def test_leaves_source_intact(self):
        migrate.migrate(self.source, self.target)
        for name in migrate.STATE_FILES:
            self.assertTrue((self.source / name).is_file())
        self.assertTrue((self.source / "memory" / "user_role.md").is_file())

    def test_creates_umbrella_gitignore(self):
        report = migrate.migrate(self.source, self.target)
        umbrella_gitignore = self.target.parent / ".gitignore"
        self.assertTrue(umbrella_gitignore.is_file())
        self.assertEqual(
            umbrella_gitignore.read_text(encoding="utf-8").strip(), "*"
        )
        self.assertTrue(report["umbrella_gitignore_created"])

    def test_preexisting_umbrella_gitignore_is_not_overwritten(self):
        umbrella = self.target.parent
        umbrella.mkdir(parents=True)
        (umbrella / ".gitignore").write_text("custom\n", encoding="utf-8")
        report = migrate.migrate(self.source, self.target)
        self.assertFalse(report["umbrella_gitignore_created"])
        self.assertEqual(
            (umbrella / ".gitignore").read_text(encoding="utf-8").strip(),
            "custom",
        )


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.source = self.tmp_path / "legacy"
        self.target = self.tmp_path / "repo" / ".apiary" / "scribe"
        self.source.mkdir()
        (self.source / "notes.jsonl").write_text(
            '{"id":1}\n', encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_rerun_reports_exists_without_force(self):
        migrate.migrate(self.source, self.target)
        report = migrate.migrate(self.source, self.target)
        self.assertEqual(report["files"]["notes.jsonl"], "exists")

    def test_rerun_does_not_clobber_without_force(self):
        migrate.migrate(self.source, self.target)
        # Simulate new-layout edits on the target side
        (self.target / "notes.jsonl").write_text(
            '{"id":1, "edited":true}\n', encoding="utf-8"
        )
        migrate.migrate(self.source, self.target)
        self.assertIn(
            '"edited":true',
            (self.target / "notes.jsonl").read_text(encoding="utf-8"),
        )

    def test_force_overwrites_target(self):
        migrate.migrate(self.source, self.target)
        (self.target / "notes.jsonl").write_text(
            "stale\n", encoding="utf-8"
        )
        report = migrate.migrate(self.source, self.target, force=True)
        self.assertEqual(report["files"]["notes.jsonl"], "copied")
        self.assertEqual(
            (self.target / "notes.jsonl").read_text(encoding="utf-8"),
            '{"id":1}\n',
        )


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.source = self.tmp_path / "legacy"
        self.target = self.tmp_path / "repo" / ".apiary" / "scribe"
        self.source.mkdir()
        (self.source / "notes.jsonl").write_text("{}", encoding="utf-8")
        (self.source / "memory").mkdir()
        (self.source / "memory" / "x.md").write_text("x", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_dry_run_does_not_create_target(self):
        report = migrate.migrate(self.source, self.target, dry_run=True)
        self.assertFalse(self.target.exists())
        self.assertTrue(report["dry_run"])

    def test_dry_run_reports_would_copy(self):
        report = migrate.migrate(self.source, self.target, dry_run=True)
        self.assertEqual(report["files"]["notes.jsonl"], "would-copy")
        self.assertEqual(report["memory"].get("would-copy", 0), 1)

    def test_dry_run_does_not_create_umbrella_gitignore(self):
        migrate.migrate(self.source, self.target, dry_run=True)
        self.assertFalse((self.target.parent / ".gitignore").exists())


class MissingSourceTests(unittest.TestCase):
    def test_reports_missing_source_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "does-not-exist"
            target = tmp_path / "target"
            report = migrate.migrate(source, target)
            self.assertFalse(report["source_exists"])
            self.assertFalse(target.exists())

    def test_missing_state_files_reported(self):
        """Source exists but individual files don't — each file reported as missing."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "legacy"
            source.mkdir()
            # Only one of the four state files present
            (source / "notes.jsonl").write_text("{}", encoding="utf-8")
            target = tmp_path / "target"
            report = migrate.migrate(source, target)
            self.assertEqual(report["files"]["notes.jsonl"], "copied")
            self.assertEqual(report["files"]["notes_archive.jsonl"], "missing")
            self.assertEqual(report["files"]["learnings.jsonl"], "missing")
            self.assertEqual(report["files"]["backfill_skip.json"], "missing")


if __name__ == "__main__":
    unittest.main()
