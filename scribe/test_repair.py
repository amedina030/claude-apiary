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

from scribe.store import (
    ScribeStore, TYPE_FOLDERS, LEARNING_FOLDER, INDEX_FILENAME,
    ARCHIVE_DIRNAME, NEXT_SEQ_FILENAME,
)
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
        year, seq = entry['year'], entry['seq']

        # Wipe the index but keep the .md file
        year_dir = self.state_dir / 'todos' / str(year)
        (year_dir / INDEX_FILENAME).write_text('', encoding='utf-8')

        self._run_repair()

        entries = ScribeStore._read_index(year_dir)
        seqs = [e['seq'] for e in entries]
        self.assertIn(seq, seqs)
        rebuilt = next(e for e in entries if e['seq'] == seq)
        self.assertIn('rebuild me please', rebuilt['summary'])

    def test_detects_orphan_entry(self):
        entry = self.store.add_note('todo', 'orphan me', 'sess1')
        year, seq = entry['year'], entry['seq']

        # Delete the .md file but leave the index entry intact
        year_dir = self.state_dir / 'todos' / str(year)
        (year_dir / f'{seq}.md').unlink()

        self._run_repair()

        entries = ScribeStore._read_index(year_dir)
        seqs = [e['seq'] for e in entries]
        self.assertNotIn(seq, seqs)

    def test_resets_next_seq(self):
        year_dir = self.state_dir / 'todos' / '2026'
        year_dir.mkdir(parents=True, exist_ok=True)

        # Write an .md file with seq=99 and a matching index entry
        (year_dir / '99.md').write_text('high seq note', encoding='utf-8')
        ScribeStore._append_index(year_dir, {
            'display_id': 'T-2026-99',
            'type': 'todo',
            'year': 2026,
            'seq': 99,
            'status': 'active',
            'session': '',
            'timestamp': '2026-01-01T00:00:00+00:00',
            'summary': 'high seq note',
            'has_body': True,
        })

        # Manually corrupt next_seq to 1
        (year_dir / NEXT_SEQ_FILENAME).write_text('1', encoding='utf-8')
        # Ensure archive subdir exists for repair scan
        archive = year_dir / ARCHIVE_DIRNAME
        archive.mkdir(exist_ok=True)
        (archive / INDEX_FILENAME).write_text('', encoding='utf-8')

        self._run_repair()

        next_seq = int((year_dir / NEXT_SEQ_FILENAME).read_text(encoding='utf-8').strip())
        self.assertEqual(next_seq, 100)

    def test_dry_run_makes_no_changes(self):
        entry = self.store.add_note('todo', 'dry run test', 'sess1')
        year, seq = entry['year'], entry['seq']

        year_dir = self.state_dir / 'todos' / str(year)
        idx_path = year_dir / INDEX_FILENAME

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
        year, seq = entry['year'], entry['seq']
        self.store.archive_note('todo', year, seq)

        archive_dir = self.state_dir / 'todos' / str(year) / ARCHIVE_DIRNAME
        (archive_dir / INDEX_FILENAME).write_text('', encoding='utf-8')

        self._run_repair()

        entries = ScribeStore._read_index(archive_dir)
        seqs = [e['seq'] for e in entries]
        self.assertIn(seq, seqs)

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
        entry = self.store.add_note('todo', 'ensure year dir exists', 'sess1')
        year_dir = self.state_dir / 'todos' / str(entry['year'])
        (year_dir / 'notes.md').write_text('stray file', encoding='utf-8')

        err = io.StringIO()
        out = io.StringIO()
        args = Namespace(dry_run=False, store=self.store)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            notes_mod.cmd_repair(args)

        self.assertIn('skipping non-integer filename', err.getvalue())


if __name__ == '__main__':
    unittest.main()
