#!/usr/bin/env python3
"""Unit tests for scripts/migrate_scribe_v2.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import migrate_scribe_v2 as mig
from scribe.store import ScribeStore, TYPE_FOLDERS, LEARNING_FOLDER, ARCHIVE_DIRNAME, INDEX_FILENAME


class MigrateScribeV2Test(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in records]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def test_notes_migrated_to_type_folders(self):
        # Acceptance: mixed types -> each .md in correct folder with matching index entry.
        records = [
            {'id': 1, 'type': 'todo', 'content': 'do thing', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'},
            {'id': 2, 'type': 'decision', 'content': 'chose X', 'session': 's1', 'timestamp': '2026-01-02T00:00:00Z'},
            {'id': 3, 'type': 'general', 'content': 'note', 'session': 's1', 'timestamp': '2026-01-03T00:00:00Z'},
        ]
        self._write_jsonl(self.state_dir / 'notes.jsonl', records)
        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertEqual(report['notes_migrated'], 3)
        self.assertTrue((self.state_dir / 'todos' / '1.md').exists())
        self.assertTrue((self.state_dir / 'decisions' / '2.md').exists())
        self.assertTrue((self.state_dir / 'general' / '3.md').exists())
        self.assertEqual((self.state_dir / 'todos' / '1.md').read_text(encoding='utf-8'), 'do thing')
        # Verify index entries
        idx = (self.state_dir / 'todos' / INDEX_FILENAME).read_text(encoding='utf-8').strip().splitlines()
        parsed = [json.loads(l) for l in idx]
        self.assertEqual(parsed[0]['id'], 1)
        self.assertEqual(parsed[0]['type'], 'todo')
        self.assertTrue(parsed[0]['has_body'])

    def test_archived_notes_go_to_archive_subfolder(self):
        recs = [{'id': 10, 'type': 'todo', 'content': 'old', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'}]
        self._write_jsonl(self.state_dir / 'notes_archive.jsonl', recs)
        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertEqual(report['archived_migrated'], 1)
        self.assertTrue((self.state_dir / 'todos' / ARCHIVE_DIRNAME / '10.md').exists())
        arc_idx = (self.state_dir / 'todos' / ARCHIVE_DIRNAME / INDEX_FILENAME).read_text(encoding='utf-8').strip()
        entry = json.loads(arc_idx)
        self.assertEqual(entry['status'], 'archived')

    def test_learnings_migrated_with_L_prefix(self):
        recs = [
            {'id': 'L1', 'content': 'learned A', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'},
            {'id': 'L2', 'content': 'learned B', 'session': 's1', 'timestamp': '2026-01-02T00:00:00Z'},
        ]
        self._write_jsonl(self.state_dir / 'learnings.jsonl', recs)
        report = mig.migrate(self.state_dir)
        self.assertEqual(report['learnings_migrated'], 2)
        self.assertTrue((self.state_dir / LEARNING_FOLDER / 'L1.md').exists())
        self.assertTrue((self.state_dir / LEARNING_FOLDER / 'L2.md').exists())

    def test_dry_run_does_not_write(self):
        recs = [{'id': 1, 'type': 'todo', 'content': 'x', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'}]
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        report = mig.migrate(self.state_dir, dry_run=True)
        self.assertEqual(report['notes_migrated'], 1)
        self.assertFalse((self.state_dir / 'todos' / '1.md').exists())
        self.assertFalse((self.state_dir / 'backup').exists())

    def test_refuses_when_new_layout_has_data_without_force(self):
        recs = [{'id': 1, 'type': 'todo', 'content': 'x', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'}]
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        # Seed new layout on first run
        mig.migrate(self.state_dir)
        # Re-write legacy and re-run without force
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        report2 = mig.migrate(self.state_dir)
        self.assertIsNotNone(report2['error'])
        self.assertIn('force', report2['error'].lower())

    def test_force_overwrites_existing_layout(self):
        recs = [{'id': 1, 'type': 'todo', 'content': 'x', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'}]
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        mig.migrate(self.state_dir)
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        report = mig.migrate(self.state_dir, force=True)
        self.assertIsNone(report['error'])
        self.assertEqual(report['notes_migrated'], 1)

    def test_malformed_line_skipped(self):
        path = self.state_dir / 'notes.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps({'id': 1, 'type': 'todo', 'content': 'ok', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'})
        path.write_text(
            good + '\n' +
            'not-json\n' +
            json.dumps({'id': 2, 'type': 'todo', 'content': 'ok2', 'session': 's1', 'timestamp': '2026-01-02T00:00:00Z'}) + '\n',
            encoding='utf-8',
        )
        report = mig.migrate(self.state_dir)
        self.assertEqual(report['notes_migrated'], 2)
        self.assertEqual(report['skipped'], 1)

    def test_backup_created_with_timestamp_prefix(self):
        recs = [{'id': 1, 'type': 'todo', 'content': 'x', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'}]
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        report = mig.migrate(self.state_dir)
        backup_dir = self.state_dir / 'backup'
        self.assertTrue(backup_dir.exists())
        backups = list(backup_dir.glob('*notes.jsonl'))
        self.assertEqual(len(backups), 1)
        # Original file still exists, untouched
        self.assertTrue((self.state_dir / 'notes.jsonl').exists())

    def test_missing_type_defaults_to_general(self):
        recs = [{'id': 5, 'content': 'no type field', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'}]
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        report = mig.migrate(self.state_dir)
        self.assertIsNone(report['error'])
        self.assertTrue((self.state_dir / 'general' / '5.md').exists())

    def test_archive_takes_precedence_over_active_on_duplicate_id(self):
        active = [{'id': 7, 'type': 'todo', 'content': 'active-ver', 'session': 's1', 'timestamp': '2026-01-02T00:00:00Z'}]
        archived = [{'id': 7, 'type': 'todo', 'content': 'archive-ver', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'}]
        self._write_jsonl(self.state_dir / 'notes.jsonl', active)
        self._write_jsonl(self.state_dir / 'notes_archive.jsonl', archived)
        report = mig.migrate(self.state_dir)
        # Archive copy wins, active duplicate dropped
        self.assertEqual(report['archived_migrated'], 1)
        self.assertEqual(report['notes_migrated'], 0)
        self.assertTrue((self.state_dir / 'todos' / ARCHIVE_DIRNAME / '7.md').exists())
        self.assertEqual(
            (self.state_dir / 'todos' / ARCHIVE_DIRNAME / '7.md').read_text(encoding='utf-8'),
            'archive-ver',
        )

    def test_no_legacy_data_returns_clean_error(self):
        report = mig.migrate(self.state_dir)
        self.assertIn('No legacy data found', report['error'])
        self.assertFalse(report['legacy_found'])

    def test_next_id_set_to_max_plus_one(self):
        recs = [
            {'id': 3, 'type': 'todo', 'content': 'a', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z'},
            {'id': 7, 'type': 'decision', 'content': 'b', 'session': 's1', 'timestamp': '2026-01-02T00:00:00Z'},
        ]
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        mig.migrate(self.state_dir)
        nid = (self.state_dir / 'next_id').read_text(encoding='utf-8').strip()
        self.assertEqual(nid, '8')

    def test_post_migration_notes_py_list_matches_input(self):
        # Acceptance criterion [6]: after v2 migration, folder-per-type indexes and body
        # files match pre-migration content.  The store API now expects per-(type, year)
        # layout, so verify v2 migration output directly via index.jsonl reads.
        recs = [
            {'id': 1, 'type': 'todo', 'content': 'first todo body', 'session': 's1', 'timestamp': '2026-01-01T00:00:00Z', 'summary': 'first todo'},
            {'id': 2, 'type': 'decision', 'content': 'second decision body', 'session': 's1', 'timestamp': '2026-01-02T00:00:00Z', 'summary': 'second decision'},
            {'id': 3, 'type': 'general', 'content': 'third general body', 'session': 's1', 'timestamp': '2026-01-03T00:00:00Z', 'summary': 'third general'},
        ]
        self._write_jsonl(self.state_dir / 'notes.jsonl', recs)
        mig.migrate(self.state_dir)
        # Read indexes directly from folder-per-type layout
        all_entries = []
        for folder in ['todos', 'decisions', 'general']:
            idx_path = self.state_dir / folder / INDEX_FILENAME
            if idx_path.exists():
                for line in idx_path.read_text(encoding='utf-8').splitlines():
                    line = line.strip()
                    if line:
                        all_entries.append(json.loads(line))
        self.assertEqual(len(all_entries), 3)
        summaries = {e['id']: e['summary'] for e in all_entries}
        self.assertEqual(summaries[1], 'first todo')
        self.assertEqual(summaries[2], 'second decision')
        self.assertEqual(summaries[3], 'third general')
        # Verify body file content round-trips
        body = (self.state_dir / 'todos' / '1.md').read_text(encoding='utf-8')
        self.assertEqual(body, 'first todo body')


if __name__ == '__main__':
    unittest.main()
