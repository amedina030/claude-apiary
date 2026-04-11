"""Tests for the repair subcommand in scribe/notes.py."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scribe.store import ScribeStore, TYPE_FOLDERS, LEARNING_FOLDER, INDEX_FILENAME, ARCHIVE_DIRNAME
import scribe.notes as notes_mod


class RepairTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.state_dir = self.tmpdir / 'scribe'
        self.store = ScribeStore(self.state_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_repair(self, dry_run=False):
        """Call cmd_repair directly, capturing stdout and stderr."""
        out = io.StringIO()
        err = io.StringIO()
        args = Namespace(dry_run=dry_run, store=self.store)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = notes_mod.cmd_repair(args)
        return result, out.getvalue(), err.getvalue()

    def test_rebuilds_missing_index_entry(self):
        entry = self.store.add_note('todo', 'rebuild me please', 'sess1')
        note_id = entry['id']

        # Wipe the index but keep the .md file
        todos_dir = self.state_dir / 'todos'
        (todos_dir / INDEX_FILENAME).write_text('', encoding='utf-8')

        self._run_repair()

        entries = ScribeStore._read_index(todos_dir)
        ids = [e['id'] for e in entries]
        self.assertIn(note_id, ids)
        rebuilt = next(e for e in entries if e['id'] == note_id)
        self.assertIn('rebuild me please', rebuilt['summary'])

    def test_detects_orphan_entry(self):
        entry = self.store.add_note('todo', 'orphan me', 'sess1')
        note_id = entry['id']

        # Delete the .md file but leave the index entry intact
        todos_dir = self.state_dir / 'todos'
        (todos_dir / f'{note_id}.md').unlink()

        self._run_repair()

        entries = ScribeStore._read_index(todos_dir)
        ids = [e['id'] for e in entries]
        self.assertNotIn(note_id, ids)

    def test_resets_next_id(self):
        todos_dir = self.state_dir / 'todos'

        # Write an .md file with id=99 and a matching index entry
        (todos_dir / '99.md').write_text('high id note', encoding='utf-8')
        ScribeStore._append_index(todos_dir, {
            'id': 99,
            'type': 'todo',
            'status': 'active',
            'session': '',
            'timestamp': '2026-01-01T00:00:00+00:00',
            'summary': 'high id note',
            'has_body': True,
        })

        # Manually corrupt next_id to 1
        (self.state_dir / 'next_id').write_text('1', encoding='utf-8')

        self._run_repair()

        next_id = int((self.state_dir / 'next_id').read_text(encoding='utf-8').strip())
        self.assertEqual(next_id, 100)

    def test_dry_run_makes_no_changes(self):
        entry = self.store.add_note('todo', 'dry run test', 'sess1')
        note_id = entry['id']

        todos_dir = self.state_dir / 'todos'
        idx_path = todos_dir / INDEX_FILENAME

        # Delete index entry, keep .md
        idx_path.write_text('', encoding='utf-8')

        before_content = idx_path.read_text(encoding='utf-8')
        before_mtime = idx_path.stat().st_mtime

        _, stdout, _ = self._run_repair(dry_run=True)

        after_content = idx_path.read_text(encoding='utf-8')
        after_mtime = idx_path.stat().st_mtime

        self.assertEqual(before_content, after_content)
        self.assertEqual(before_mtime, after_mtime)
        self.assertIn('dry-run', stdout)

    def test_repair_archive_subfolder(self):
        entry = self.store.add_note('todo', 'archive repair test', 'sess1')
        note_id = entry['id']
        self.store.archive_note(note_id)

        archive_dir = self.state_dir / 'todos' / ARCHIVE_DIRNAME
        (archive_dir / INDEX_FILENAME).write_text('', encoding='utf-8')

        self._run_repair()

        entries = ScribeStore._read_index(archive_dir)
        ids = [e['id'] for e in entries]
        self.assertIn(note_id, ids)

    def test_empty_state_dir(self):
        # Create ScribeStore pointing at an empty dir, then delete all subfolders
        empty_dir = self.tmpdir / 'empty_scribe'
        empty_store = ScribeStore(empty_dir)
        # Remove everything under empty_dir
        shutil.rmtree(empty_dir)

        args = Namespace(dry_run=False, store=empty_store)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            notes_mod.cmd_repair(args)

        self.assertIn('No scribe data found', out.getvalue())

    def test_non_integer_md_filename_skipped(self):
        todos_dir = self.state_dir / 'todos'
        (todos_dir / 'notes.md').write_text('stray file', encoding='utf-8')

        err = io.StringIO()
        out = io.StringIO()
        args = Namespace(dry_run=False, store=self.store)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            notes_mod.cmd_repair(args)

        self.assertIn('skipping non-integer filename', err.getvalue())


if __name__ == '__main__':
    unittest.main()
