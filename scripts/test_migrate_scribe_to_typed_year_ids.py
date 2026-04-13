#!/usr/bin/env python3
"""Unit tests for scripts/migrate_scribe_to_typed_year_ids.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import migrate_scribe_to_typed_year_ids as mig
from scribe.store import TYPE_FOLDERS, LEARNING_FOLDER, ARCHIVE_DIRNAME, INDEX_FILENAME, NEXT_SEQ_FILENAME


class MigrateTypedYearIdsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Nest state_dir as <tmp>/.apiary/scribe so that repo_root = <tmp>
        # and glob patterns like '.apiary/scribe/memory/*.md' resolve correctly.
        self.repo_root = Path(self._tmp.name)
        self.state_dir = self.repo_root / '.apiary' / 'scribe'
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Create basic v2 folder structure (what migrate_scribe_v2 would have produced)
        for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
            folder = self.state_dir / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / INDEX_FILENAME).write_text('', encoding='utf-8')
            archive = folder / ARCHIVE_DIRNAME
            archive.mkdir(parents=True, exist_ok=True)
            (archive / INDEX_FILENAME).write_text('', encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def _write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in records]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records

    # --- AC: basic renumbering ---
    def test_basic_renumbering_two_todos_same_year(self):
        """AC1: two todos with 2026 timestamps get seq=1,2 sorted by timestamp."""
        records = [
            {'id': 5, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-03-15T00:00:00Z', 'summary': 'later', 'has_body': True},
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'first', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body of 1', encoding='utf-8')
        (self.state_dir / 'todos' / '5.md').write_text('body of 5', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertEqual(report['notes_migrated'], 2)

        # Check year index
        idx = self._read_jsonl(self.state_dir / 'todos' / '2026' / INDEX_FILENAME)
        self.assertEqual(len(idx), 2)
        self.assertEqual(idx[0]['display_id'], 'T-2026-1')
        self.assertEqual(idx[0]['seq'], 1)
        self.assertEqual(idx[1]['display_id'], 'T-2026-2')
        self.assertEqual(idx[1]['seq'], 2)

        # Check next_seq
        ns = (self.state_dir / 'todos' / '2026' / NEXT_SEQ_FILENAME).read_text(encoding='utf-8').strip()
        self.assertEqual(ns, '3')

    # --- AC: body file relocation ---
    def test_body_file_moved_to_year_subfolder(self):
        """AC2: body .md file moved from <type>/<id>.md to <type>/<year>/<seq>.md."""
        records = [
            {'id': 42, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-06-01T00:00:00Z', 'summary': 'buy milk', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '42.md').write_text('buy milk', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        # New body file exists with correct content
        new_body = self.state_dir / 'todos' / '2026' / '1.md'
        self.assertTrue(new_body.exists())
        self.assertEqual(new_body.read_text(encoding='utf-8'), 'buy milk')
        # Old body file cleaned up
        self.assertFalse((self.state_dir / 'todos' / '42.md').exists())

    # --- AC: multi-year splitting ---
    def test_notes_spanning_multiple_years_get_separate_folders(self):
        """AC3: notes spanning 2025 and 2026 get separate year subfolders with independent seq."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2025-06-01T00:00:00Z', 'summary': 'old', 'has_body': True},
            {'id': 2, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-03-01T00:00:00Z', 'summary': 'new', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('old body', encoding='utf-8')
        (self.state_dir / 'todos' / '2.md').write_text('new body', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        # 2025 folder
        idx_2025 = self._read_jsonl(self.state_dir / 'todos' / '2025' / INDEX_FILENAME)
        self.assertEqual(len(idx_2025), 1)
        self.assertEqual(idx_2025[0]['display_id'], 'T-2025-1')
        # 2026 folder
        idx_2026 = self._read_jsonl(self.state_dir / 'todos' / '2026' / INDEX_FILENAME)
        self.assertEqual(len(idx_2026), 1)
        self.assertEqual(idx_2026[0]['display_id'], 'T-2026-1')

    # --- AC: migration_id_map for learnings ---
    def test_migration_map_learnings_dual_keys(self):
        """AC4 + AC18: learning IDs get both 'L<n>' and '<n>' keys in migration map."""
        records = [
            {'id': 'L1', 'type': 'learning', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'learned', 'has_body': True},
            {'id': 'L3', 'type': 'learning', 'status': 'active', 'session': 's1',
             'timestamp': '2026-02-01T00:00:00Z', 'summary': 'learned2', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'learnings' / INDEX_FILENAME, records)
        (self.state_dir / 'learnings' / 'L1.md').write_text('content1', encoding='utf-8')
        (self.state_dir / 'learnings' / 'L3.md').write_text('content3', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])

        map_path = self.state_dir / 'migration_id_map.json'
        self.assertTrue(map_path.exists())
        id_map = json.loads(map_path.read_text(encoding='utf-8'))
        # Both L-prefixed and bare numeric keys
        self.assertIn('L1', id_map)
        self.assertIn('1', id_map)
        self.assertIn('L3', id_map)
        self.assertIn('3', id_map)
        self.assertEqual(id_map['L1'], id_map['1'])
        self.assertEqual(id_map['L3'], id_map['3'])

    # --- AC: reference rewriting in memory ---
    def test_reference_rewriting_in_memory_file(self):
        """AC5: '#188' in memory file is rewritten to new display_id."""
        # Create note with id=188
        records = [
            {'id': 188, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'important', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '188.md').write_text('body', encoding='utf-8')

        # Create memory file at .apiary/scribe/memory/ — matches the scan glob
        # '.apiary/scribe/memory/*.md' from repo_root = state_dir.parent.parent
        mem_dir = self.state_dir / 'memory'
        mem_dir.mkdir(parents=True, exist_ok=True)
        mem_file = mem_dir / 'some-fact.md'
        mem_file.write_text('see scribe note #188 for details', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])

        # Verify reference was rewritten
        content = mem_file.read_text(encoding='utf-8')
        self.assertIn('T-2026-1', content)
        self.assertNotIn('#188', content)

    # --- AC: reference rewriting notes.py get ---
    def test_reference_rewriting_notes_py_get(self):
        """AC7: 'notes.py get 42' is rewritten to 'notes.py get T-2026-1'."""
        records = [
            {'id': 42, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '42.md').write_text('body', encoding='utf-8')

        mem_dir = self.state_dir / 'memory'
        mem_dir.mkdir(parents=True, exist_ok=True)
        doc = mem_dir / 'usage.md'
        doc.write_text('run notes.py get 42 to see it', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        content = doc.read_text(encoding='utf-8')
        self.assertIn('notes.py get T-2026-1', content)
        self.assertNotIn('notes.py get 42', content)

    # --- AC: unresolved refs left unchanged ---
    def test_unresolved_ref_left_unchanged(self):
        """AC8: '#999' not in map is left unchanged, unresolved_refs incremented."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')

        mem_dir = self.state_dir / 'memory'
        mem_dir.mkdir(parents=True, exist_ok=True)
        doc = mem_dir / 'ref.md'
        doc.write_text('see #999 and #1', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        content = doc.read_text(encoding='utf-8')
        self.assertIn('#999', content)  # unchanged
        self.assertIn('T-2026-1', content)  # rewritten
        self.assertGreaterEqual(report['unresolved_refs'], 1)

    # --- AC: dry-run no-op ---
    def test_dry_run_does_not_write(self):
        """AC9: --dry-run does not create, move, or modify files."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')

        report = mig.migrate(self.state_dir, dry_run=True)
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['notes_migrated'], 1)
        self.assertIsNone(report['error'])
        # No year subfolder created
        self.assertFalse((self.state_dir / 'todos' / '2026' / INDEX_FILENAME).exists())
        # No backup created
        self.assertFalse((self.state_dir / 'backup').exists())
        # No migration map
        self.assertFalse((self.state_dir / 'migration_id_map.json').exists())
        # Old files untouched
        self.assertTrue((self.state_dir / 'todos' / '1.md').exists())

    # --- AC: refuses without --force ---
    def test_refuses_when_year_data_exists_without_force(self):
        """AC10: second run without --force returns error about existing data."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')
        mig.migrate(self.state_dir)

        # Re-seed top-level index for second run
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        report2 = mig.migrate(self.state_dir)
        self.assertIsNotNone(report2['error'])
        self.assertIn('already contains data', report2['error'])

    # --- AC: idempotent with --force ---
    def test_force_remigration_is_idempotent(self):
        """AC11: migrating twice with --force produces identical output."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'first', 'has_body': True},
            {'id': 2, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-02-01T00:00:00Z', 'summary': 'second', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body1', encoding='utf-8')
        (self.state_dir / 'todos' / '2.md').write_text('body2', encoding='utf-8')

        report1 = mig.migrate(self.state_dir)
        map1 = json.loads((self.state_dir / 'migration_id_map.json').read_text(encoding='utf-8'))
        idx1 = self._read_jsonl(self.state_dir / 'todos' / '2026' / INDEX_FILENAME)

        # Re-seed and force-remigrate
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body1', encoding='utf-8')
        (self.state_dir / 'todos' / '2.md').write_text('body2', encoding='utf-8')
        report2 = mig.migrate(self.state_dir, force=True)
        map2 = json.loads((self.state_dir / 'migration_id_map.json').read_text(encoding='utf-8'))
        idx2 = self._read_jsonl(self.state_dir / 'todos' / '2026' / INDEX_FILENAME)

        self.assertEqual(map1, map2)
        self.assertEqual(len(idx1), len(idx2))
        for e1, e2 in zip(idx1, idx2):
            self.assertEqual(e1['display_id'], e2['display_id'])
            self.assertEqual(e1['seq'], e2['seq'])

    # --- AC: archive takes precedence on duplicate id ---
    def test_archive_takes_precedence_on_duplicate_id(self):
        """AC12: if same id in active and archive, archive wins."""
        active = [
            {'id': 7, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-02T00:00:00Z', 'summary': 'active-ver', 'has_body': True},
        ]
        archived = [
            {'id': 7, 'type': 'todo', 'status': 'archived', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'archive-ver', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, active)
        self._write_jsonl(self.state_dir / 'todos' / ARCHIVE_DIRNAME / INDEX_FILENAME, archived)
        (self.state_dir / 'todos' / '7.md').write_text('active body', encoding='utf-8')
        (self.state_dir / 'todos' / ARCHIVE_DIRNAME / '7.md').write_text('archive body', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertEqual(report['duplicates_dropped'], 1)
        self.assertEqual(report['archived_migrated'], 1)
        self.assertEqual(report['notes_migrated'], 0)

    # --- AC: identical timestamps preserve order ---
    def test_identical_timestamps_preserve_order(self):
        """AC13: entries with identical timestamps retain original index order."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'first', 'has_body': True},
            {'id': 2, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'second', 'has_body': True},
            {'id': 3, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'third', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        for r in records:
            (self.state_dir / 'todos' / f"{r['id']}.md").write_text(f"body {r['id']}", encoding='utf-8')

        report = mig.migrate(self.state_dir)
        idx = self._read_jsonl(self.state_dir / 'todos' / '2026' / INDEX_FILENAME)
        self.assertEqual(idx[0]['seq'], 1)
        self.assertEqual(idx[0]['summary'], 'first')
        self.assertEqual(idx[1]['seq'], 2)
        self.assertEqual(idx[1]['summary'], 'second')
        self.assertEqual(idx[2]['seq'], 3)
        self.assertEqual(idx[2]['summary'], 'third')

    # --- AC: missing body file ---
    def test_missing_body_file_creates_empty(self):
        """AC14: missing body .md creates empty file and increments missing_body_count."""
        records = [
            {'id': 10, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'no body', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        # Deliberately do NOT create 10.md

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertGreaterEqual(report['missing_body_count'], 1)
        # Empty body file created
        new_body = self.state_dir / 'todos' / '2026' / '1.md'
        self.assertTrue(new_body.exists())
        self.assertEqual(new_body.read_text(encoding='utf-8'), '')

    # --- AC: backup snapshot ---
    def test_backup_snapshot_created(self):
        """AC15: backup contains pre-migration type folders and indexes."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNotNone(report['backup_dir'])
        backup_dir = Path(report['backup_dir'])
        self.assertTrue(backup_dir.exists())
        # Backup contains todos folder with index and body file
        self.assertTrue((backup_dir / 'todos' / INDEX_FILENAME).exists())
        self.assertTrue((backup_dir / 'todos' / '1.md').exists())

    # --- AC: no v2 data ---
    def test_no_v2_data_returns_error(self):
        """AC: error when state_dir has empty indexes."""
        report = mig.migrate(self.state_dir)
        self.assertIn('No v2 data found', report['error'])

    # --- AC: missing timestamp fallback ---
    def test_missing_timestamp_falls_back_to_current_year(self):
        """AC17: note without timestamp gets bucketed under current UTC year."""
        from datetime import datetime, timezone
        current_year = datetime.now(timezone.utc).year
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'summary': 'no ts', 'has_body': True},  # no timestamp field
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertGreaterEqual(report['timestamp_fallback_count'], 1)
        idx = self._read_jsonl(self.state_dir / 'todos' / str(current_year) / INDEX_FILENAME)
        self.assertEqual(len(idx), 1)

    # --- AC: migration_id_map for notes ---
    def test_migration_map_for_notes(self):
        """AC4: integer note IDs map correctly in migration_id_map.json."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'a', 'has_body': True},
            {'id': 5, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-02-01T00:00:00Z', 'summary': 'b', 'has_body': True},
            {'id': 42, 'type': 'decision', 'status': 'active', 'session': 's1',
             'timestamp': '2026-03-01T00:00:00Z', 'summary': 'c', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records[:2])
        self._write_jsonl(self.state_dir / 'decisions' / INDEX_FILENAME, records[2:])
        (self.state_dir / 'todos' / '1.md').write_text('a', encoding='utf-8')
        (self.state_dir / 'todos' / '5.md').write_text('b', encoding='utf-8')
        (self.state_dir / 'decisions' / '42.md').write_text('c', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        id_map = json.loads((self.state_dir / 'migration_id_map.json').read_text(encoding='utf-8'))
        self.assertIn('1', id_map)
        self.assertIn('5', id_map)
        self.assertIn('42', id_map)
        self.assertEqual(id_map['1'], 'T-2026-1')
        self.assertEqual(id_map['5'], 'T-2026-2')
        self.assertEqual(id_map['42'], 'D-2026-1')

    # --- AC: old files cleaned up ---
    def test_old_files_cleaned_up(self):
        """Old top-level index.jsonl, integer .md files, and next_id removed after migration."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')
        # Create a next_id file
        (self.state_dir / 'next_id').write_text('2', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertGreater(report['old_files_removed'], 0)
        self.assertFalse((self.state_dir / 'todos' / '1.md').exists())
        self.assertFalse((self.state_dir / 'next_id').exists())

    # --- AC: unparseable timestamp counted as fallback ---
    def test_unparseable_timestamp_counted_as_fallback(self):
        """ATK-001: a present-but-unparseable timestamp also increments timestamp_fallback_count."""
        from datetime import datetime, timezone
        current_year = datetime.now(timezone.utc).year
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': 'garbage', 'summary': 'bad ts', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertGreaterEqual(report['timestamp_fallback_count'], 1)
        # Falls back to current year
        idx = self._read_jsonl(self.state_dir / 'todos' / str(current_year) / INDEX_FILENAME)
        self.assertEqual(len(idx), 1)

    # --- AC: archived learnings migrated ---
    def test_archived_learnings_are_migrated(self):
        """ATK-008: learnings/archive/index.jsonl entries are collected and migrated."""
        active_records = [
            {'id': 'L1', 'type': 'learning', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'active learn', 'has_body': True},
        ]
        archived_records = [
            {'id': 'L2', 'type': 'learning', 'status': 'archived', 'session': 's1',
             'timestamp': '2026-02-01T00:00:00Z', 'summary': 'archived learn', 'has_body': True},
        ]
        learn_dir = self.state_dir / 'learnings'
        self._write_jsonl(learn_dir / INDEX_FILENAME, active_records)
        (learn_dir / 'L1.md').write_text('active body', encoding='utf-8')
        archive_dir = learn_dir / ARCHIVE_DIRNAME
        self._write_jsonl(archive_dir / INDEX_FILENAME, archived_records)
        (archive_dir / 'L2.md').write_text('archived body', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        # Both active and archived learnings counted
        self.assertEqual(report['learnings_migrated'], 2)

        # Active learning body written
        active_idx = self._read_jsonl(self.state_dir / 'learnings' / '2026' / INDEX_FILENAME)
        self.assertTrue(any(e.get('summary') == 'active learn' for e in active_idx))

        # Archived learning body written to archive subfolder
        arch_idx = self._read_jsonl(
            self.state_dir / 'learnings' / '2026' / ARCHIVE_DIRNAME / INDEX_FILENAME
        )
        self.assertTrue(any(e.get('summary') == 'archived learn' for e in arch_idx))

    # --- AC: force without re-seeding gives clear error ---
    def test_force_without_reseeding_gives_clear_error(self):
        """ATK-002: --force after a completed migration (top-level indexes removed) gives a
        clear error instead of silently migrating nothing."""
        records = [
            {'id': 1, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '1.md').write_text('body', encoding='utf-8')

        # First migration removes top-level indexes
        report1 = mig.migrate(self.state_dir)
        self.assertIsNone(report1['error'])

        # --force without re-seeding top-level indexes should error clearly, not silently migrate nothing
        report2 = mig.migrate(self.state_dir, force=True)
        self.assertIsNotNone(report2['error'])
        self.assertIn('backup', report2['error'])

    # --- AC: harden commands reference rewriting ---
    def test_reference_rewriting_in_harden_commands(self):
        """AC6: references in harden/commands/*.md are rewritten."""
        records = [
            {'id': 188, 'type': 'todo', 'status': 'active', 'session': 's1',
             'timestamp': '2026-01-01T00:00:00Z', 'summary': 'x', 'has_body': True},
        ]
        self._write_jsonl(self.state_dir / 'todos' / INDEX_FILENAME, records)
        (self.state_dir / 'todos' / '188.md').write_text('body', encoding='utf-8')

        # Create harden commands dir relative to repo root (= state_dir.parent.parent)
        harden_dir = self.repo_root / 'harden' / 'commands'
        harden_dir.mkdir(parents=True, exist_ok=True)
        harden_file = harden_dir / 'harden.md'
        harden_file.write_text('(Formula source: scribe note #188.)', encoding='utf-8')

        report = mig.migrate(self.state_dir)
        content = harden_file.read_text(encoding='utf-8')
        self.assertIn('T-2026-1', content)
        self.assertNotIn('#188', content)


if __name__ == '__main__':
    unittest.main()
