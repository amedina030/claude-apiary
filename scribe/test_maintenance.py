#!/usr/bin/env python3
"""Tests for scribe/maintenance.py — the whole-store operations.

`repair` and `backfill-brief` keep their coverage in test_repair.py (they are
exercised through the CLI handlers there); this file covers the folder walk
both of them now share, and the backup/restore pair that had no restore half
until the 2026-08 review.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scribe.notes as notes_mod
from scribe import maintenance
from scribe.store import ARCHIVE_DIRNAME, INDEX_FILENAME, ScribeStore


class IterIndexFoldersTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "scribe"
        self.store = ScribeStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_visits_active_and_archive_for_a_year(self):
        entry = self.store.add_note("todo", "a note", "s1")
        spots = [
            s
            for s in maintenance.iter_index_folders(self.state_dir)
            if s.note_type == "todo" and s.year == entry["year"]
        ]
        self.assertEqual([s.is_archive for s in spots], [False, True])

    def test_learnings_are_walked_too(self):
        self.store.add_learning("a learning", "s1")
        types = {s.note_type for s in maintenance.iter_index_folders(self.state_dir)}
        self.assertIn("learning", types)

    def test_non_year_directories_are_skipped(self):
        (self.state_dir / "todos" / "templates").mkdir(parents=True, exist_ok=True)
        names = {s.year_dir.name for s in maintenance.iter_index_folders(self.state_dir)}
        self.assertNotIn("templates", names)

    def test_folder_to_note_type_round_trips(self):
        self.assertEqual(maintenance.folder_to_note_type("todos"), "todo")
        self.assertEqual(maintenance.folder_to_note_type("learnings"), "learning")
        self.assertEqual(maintenance.folder_to_note_type("who-put-this-here"), "general")


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "scribe"
        self.store = ScribeStore(self.state_dir)
        self.root = maintenance.backups_root(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot(self, date_str="2026-08-26"):
        self.root.mkdir(parents=True, exist_ok=True)
        return maintenance.create_backup(self.state_dir, self.root, date_str)

    def _run(self, handler, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        args = Namespace(store=self.store, **kwargs)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            handler(args)
        return out.getvalue(), err.getvalue()

    def test_backup_then_restore_round_trips_an_index(self):
        entry = self.store.add_note("todo", "keep me", "s1")
        year_dir = self.state_dir / "todos" / str(entry["year"])
        self._snapshot()

        # Lose the index row (but not the body) the way a lost-update would.
        (year_dir / INDEX_FILENAME).write_text("", encoding="utf-8")
        self.assertEqual(self.store.list_notes(status="active"), [])

        report = maintenance.restore_backup(self.state_dir, "2026-08-26")
        self.assertEqual(report.source.name, "2026-08-26")
        self.assertGreaterEqual(report.restored, 1)
        restored = self.store.list_notes(status="active")
        self.assertEqual([n["seq"] for n in restored], [entry["seq"]])

    def test_restore_defaults_to_the_newest_snapshot(self):
        self.store.add_note("todo", "first", "s1")
        self._snapshot("2026-08-01")
        self.store.add_note("todo", "second", "s1")
        self._snapshot("2026-08-20")

        report = maintenance.restore_backup(self.state_dir)
        self.assertEqual(report.source.name, "2026-08-20")
        self.assertEqual(len(self.store.list_notes(status="active")), 2)

    def test_restore_of_an_older_snapshot_drops_the_newer_row(self):
        # The row is dropped, the body is not — that is what `repair` rebuilds.
        first = self.store.add_note("todo", "first", "s1")
        self._snapshot("2026-08-01")
        second = self.store.add_note("todo", "second", "s1")

        maintenance.restore_backup(self.state_dir, "2026-08-01")
        self.assertEqual([n["seq"] for n in self.store.list_notes(status="active")], [first["seq"]])
        body = self.state_dir / "todos" / str(second["year"]) / f"{second['seq']}.md"
        self.assertTrue(body.exists())

        notes_mod.cmd_repair(Namespace(store=self.store, dry_run=False))
        self.assertEqual(len(self.store.list_notes(status="active")), 2)

    def test_dry_run_restores_nothing(self):
        entry = self.store.add_note("todo", "keep me", "s1")
        self._snapshot()
        year_dir = self.state_dir / "todos" / str(entry["year"])
        (year_dir / INDEX_FILENAME).write_text("", encoding="utf-8")

        report = maintenance.restore_backup(self.state_dir, dry_run=True)
        self.assertGreaterEqual(report.restored, 1)
        self.assertEqual(self.store.list_notes(status="active"), [])

    def test_restore_without_any_snapshot_raises(self):
        with self.assertRaises(FileNotFoundError):
            maintenance.restore_backup(self.state_dir)

    def test_restore_of_an_unknown_date_names_what_exists(self):
        self._snapshot("2026-08-01")
        with self.assertRaises(FileNotFoundError) as ctx:
            maintenance.restore_backup(self.state_dir, "1999-01-01")
        self.assertIn("2026-08-01", str(ctx.exception))

    def test_backup_covers_archive_indexes(self):
        entry = self.store.add_note("todo", "archive me", "s1")
        self.store.archive_note("todo", entry["year"], entry["seq"])
        target, count = self._snapshot()
        self.assertGreaterEqual(count, 1)
        self.assertTrue(
            (target / "todos" / str(entry["year"]) / ARCHIVE_DIRNAME / INDEX_FILENAME).exists()
        )

    def test_prune_keeps_the_newest_n(self):
        for day in ("01", "02", "03", "04"):
            self._snapshot(f"2026-08-{day}")
        maintenance.prune_backups(self.root, retain=2)
        self.assertEqual(
            [d.name for d in maintenance.list_backups(self.state_dir)], ["2026-08-03", "2026-08-04"]
        )

    # --- CLI handlers -----------------------------------------------------

    def test_cmd_backup_reports_and_prunes(self):
        self.store.add_note("todo", "a", "s1")
        out, _ = self._run(notes_mod.cmd_backup, retain=30)
        self.assertIn("Backup created", out)
        self.assertEqual(len(maintenance.list_backups(self.state_dir)), 1)

    def test_cmd_restore_lists_snapshots(self):
        self._snapshot("2026-08-01")
        out, _ = self._run(notes_mod.cmd_restore, source=None, list=True, dry_run=False)
        self.assertIn("2026-08-01", out)

    def test_cmd_restore_points_at_repair_afterwards(self):
        self.store.add_note("todo", "a", "s1")
        self._snapshot("2026-08-01")
        out, _ = self._run(notes_mod.cmd_restore, source="2026-08-01", list=False, dry_run=False)
        self.assertIn("repair", out)

    def test_cmd_restore_without_snapshots_exits_one(self):
        with self.assertRaises(SystemExit):
            self._run(notes_mod.cmd_restore, source=None, list=False, dry_run=False)


if __name__ == "__main__":
    unittest.main()
