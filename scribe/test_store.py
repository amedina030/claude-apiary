#!/usr/bin/env python3
"""Unit tests for scribe.store — folder-per-type storage engine."""
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import ScribeStore, TYPE_FOLDERS, TYPE_PREFIXES, LEARNING_FOLDER, INDEX_FILENAME, ARCHIVE_DIRNAME, NEXT_SEQ_FILENAME


class TestEnsureLayout(unittest.TestCase):
    """AC: Given a fresh state_dir, ensure_layout creates all folders."""

    def test_creates_all_type_folders_with_index_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / 'scribe_state'
            year = datetime.now(timezone.utc).year
            store = ScribeStore(state_dir)
            all_folders = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]
            for folder_name in all_folders:
                folder = state_dir / folder_name
                self.assertTrue(folder.is_dir(), f"{folder_name} should exist")
                # Year subfolder is the only active layout
                year_dir = folder / str(year)
                self.assertTrue(year_dir.is_dir(), f"{folder_name}/{year} should exist")
                year_idx = year_dir / INDEX_FILENAME
                self.assertTrue(year_idx.exists(), f"{folder_name}/{year}/index.jsonl should exist")
                seq_path = year_dir / NEXT_SEQ_FILENAME
                self.assertTrue(seq_path.exists(), f"{folder_name}/{year}/next_seq should exist")
                self.assertEqual(seq_path.read_text(encoding='utf-8').strip(), '1')
                year_archive = year_dir / ARCHIVE_DIRNAME
                self.assertTrue(year_archive.is_dir(), f"{folder_name}/{year}/archive should exist")
                # Flat layout must NOT be created
                self.assertFalse((folder / INDEX_FILENAME).exists(), f"{folder_name}/index.jsonl (flat) should not exist")
                self.assertFalse((folder / ARCHIVE_DIRNAME).exists(), f"{folder_name}/archive (flat) should not exist")
            # next_id file must NOT be created (legacy removed)
            self.assertFalse((state_dir / 'next_id').exists())


class TestAddNote(unittest.TestCase):
    """AC: add_note increments next_seq, writes index entry and .md file."""

    def test_add_first_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            result = store.add_note('todo', 'fix bug', 'abc123')
            self.assertEqual(result['seq'], 1)
            self.assertEqual(result['year'], year)
            self.assertEqual(result['display_id'], f"T-{year}-1")
            self.assertEqual(result['type'], 'todo')
            self.assertEqual(result['status'], 'active')
            self.assertEqual(result['session'], 'abc123')
            self.assertTrue(result['has_body'])
            # Check year-dir index file
            idx_path = Path(tmp) / 'todos' / str(year) / INDEX_FILENAME
            lines = [l for l in idx_path.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry['seq'], 1)
            # Check .md file in year dir
            md_path = Path(tmp) / 'todos' / str(year) / '1.md'
            self.assertEqual(md_path.read_text(encoding='utf-8'), 'fix bug')
            # next_seq should now be 2
            seq_path = Path(tmp) / 'todos' / str(year) / NEXT_SEQ_FILENAME
            self.assertEqual(seq_path.read_text(encoding='utf-8').strip(), '2')

    def test_add_multiple_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            n1 = store.add_note('todo', 'first', 's1')
            n2 = store.add_note('decision', 'second', 's1')
            n3 = store.add_note('todo', 'third', 's1')
            # Each type has its own per-(type,year) counter
            self.assertEqual(n1['seq'], 1)
            self.assertEqual(n2['seq'], 1)  # decision counter starts at 1
            self.assertEqual(n3['seq'], 2)  # todo counter increments to 2


class TestGetNote(unittest.TestCase):
    """AC: get_note returns index metadata and .md content."""

    def test_get_existing_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_note('todo', 'fix bug', 's1')
            result = store.get_note(added['type'], added['year'], added['seq'])
            self.assertIsNotNone(result)
            self.assertEqual(result['seq'], 1)
            self.assertEqual(result['content'], 'fix bug')

    def test_get_nonexistent_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            self.assertIsNone(store.get_note('todo', year, 999))

    def test_get_note_missing_body(self):
        """Index entry exists but .md file is gone -> content=None, warning."""
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_note('todo', 'content here', 's1')
            # Delete the .md file
            md = Path(tmp) / 'todos' / str(year) / '1.md'
            md.unlink()
            result = store.get_note(added['type'], added['year'], added['seq'])
            self.assertIsNone(result['content'])
            self.assertEqual(result.get('_warning'), 'body_file_missing')


class TestListNotes(unittest.TestCase):
    """AC: list_notes returns all notes sorted by timestamp descending."""

    def test_list_all_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'a', 's1')
            store.add_note('decision', 'b', 's1')
            store.add_note('blocker', 'c', 's1')
            results = store.list_notes()
            self.assertEqual(len(results), 3)
            # Sorted by timestamp desc — last added is first
            types = [r['type'] for r in results]
            self.assertEqual(types, ['blocker', 'decision', 'todo'])

    def test_list_filtered_by_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'a', 's1')
            store.add_note('decision', 'b', 's1')
            results = store.list_notes(note_type='todo')
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['type'], 'todo')

    def test_list_with_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'fix the bug', 's1')
            store.add_note('todo', 'add feature', 's1')
            results = store.list_notes(search='bug')
            self.assertEqual(len(results), 1)
            self.assertIn('bug', results[0]['summary'])


class TestArchiveNote(unittest.TestCase):
    """AC: archive_note moves index entry and .md to year archive/."""

    def test_archive_moves_entry_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_note('todo', 'to archive', 's1')
            result = store.archive_note(added['type'], added['year'], added['seq'])
            self.assertIsNotNone(result)
            # An active note preserves its 'active' status when archived —
            # archived-ness is indicated by folder location and archived_at.
            self.assertEqual(result['status'], 'active')
            self.assertIn('archived_at', result)
            # Active year index should be empty
            active = store._read_index(Path(tmp) / 'todos' / str(year))
            self.assertEqual(len(active), 0)
            # Archive index under year dir should have the entry
            archived = store._read_index(Path(tmp) / 'todos' / str(year) / ARCHIVE_DIRNAME)
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0]['seq'], 1)
            # .md moved to year archive
            self.assertFalse((Path(tmp) / 'todos' / str(year) / '1.md').exists())
            self.assertTrue((Path(tmp) / 'todos' / str(year) / 'archive' / '1.md').exists())

    def test_archive_preserves_done_status(self):
        """Archiving a done note must preserve status='done' — the original
        bug clobbered it to 'archived', erasing completion history."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note('todo', 'finished work', 's1')
            store.update_note(added['type'], added['year'], added['seq'], status='done')
            result = store.archive_note(added['type'], added['year'], added['seq'])
            self.assertIsNotNone(result)
            self.assertEqual(result['status'], 'done')
            self.assertIn('archived_at', result)
            # And the archive index reflects the same.
            listed = store.list_notes(note_type='todo', status='archived')
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]['status'], 'done')

    def test_get_archived_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note('todo', 'archive me', 's1')
            store.archive_note(added['type'], added['year'], added['seq'])
            result = store.get_note(added['type'], added['year'], added['seq'])
            self.assertIsNotNone(result)
            # get_note returns the real status plus _from_archive flag
            self.assertEqual(result['status'], 'active')
            self.assertTrue(result.get('_from_archive'))
            self.assertEqual(result['content'], 'archive me')


class TestConcurrency(unittest.TestCase):
    """AC: concurrent add_note calls get unique seqs without corruption."""

    def test_concurrent_adds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            results = []
            errors = []

            def add(i):
                try:
                    r = store.add_note('todo', f'note {i}', 'concurrent')
                    results.append(r)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=add, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(errors), 0, f"Errors: {errors}")
            self.assertEqual(len(results), 10)
            seqs = {r['seq'] for r in results}
            self.assertEqual(len(seqs), 10, "All seqs should be unique")


class TestNextSeqRebuild(unittest.TestCase):
    """AC: next_seq file deleted -> rebuild from max seq in year index."""

    def test_rebuild_after_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'a', 's1')  # seq=1
            store.add_note('todo', 'b', 's1')  # seq=2
            # Delete next_seq for the todo year dir
            seq_path = Path(tmp) / 'todos' / str(year) / NEXT_SEQ_FILENAME
            seq_path.unlink()
            # Next add should rebuild and get seq=3
            result = store.add_note('todo', 'c', 's1')
            self.assertEqual(result['seq'], 3)


class TestMalformedIndex(unittest.TestCase):
    """AC: malformed line in index.jsonl is skipped with warning."""

    def test_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'good note', 's1')
            # Inject a bad line into the year index
            idx = Path(tmp) / 'todos' / str(year) / INDEX_FILENAME
            content = idx.read_text(encoding='utf-8')
            idx.write_text('NOT VALID JSON\n' + content, encoding='utf-8')
            # list_notes should still work, returning only the valid entry
            results = store.list_notes(note_type='todo')
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['seq'], 1)

    def test_rebuild_raises_on_malformed_index(self):
        """Rebuild must NOT silently skip bad lines — it would undercount seq.

        If next_seq is missing and the index has a malformed line covering the
        real max seq, a skipping rebuild would reset the counter below an
        already-used ID and the next write would collide.
        """
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'a', 's1')  # seq=1
            store.add_note('todo', 'b', 's1')  # seq=2
            year_dir = Path(tmp) / 'todos' / str(year)
            idx = year_dir / INDEX_FILENAME
            # Corrupt the line for seq=2 so a skipping rebuild would max at 1
            lines = idx.read_text(encoding='utf-8').splitlines()
            lines[-1] = 'NOT VALID JSON'
            idx.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            # Drop next_seq so the next write triggers a rebuild
            (year_dir / NEXT_SEQ_FILENAME).unlink()
            with self.assertRaises(RuntimeError) as ctx:
                store.add_note('todo', 'c', 's1')
            self.assertIn('Malformed index entry', str(ctx.exception))


class TestUpdateNote(unittest.TestCase):
    def test_update_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_note('todo', 'original', 's1')
            result = store.update_note(added['type'], added['year'], added['seq'], summary='updated summary')
            self.assertIsNotNone(result)
            self.assertEqual(result['summary'], 'updated summary')

    def test_update_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            added = store.add_note('todo', 'original', 's1')
            store.update_note(added['type'], added['year'], added['seq'], content='new body')
            md = Path(tmp) / 'todos' / str(year) / '1.md'
            self.assertEqual(md.read_text(encoding='utf-8'), 'new body')


class TestLearnings(unittest.TestCase):
    def test_add_and_list_learnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            l1 = store.add_learning('learned thing 1', 's1')
            l2 = store.add_learning('learned thing 2', 's1')
            self.assertEqual(l1['type'], 'learning')
            results = store.list_learnings()
            self.assertEqual(len(results), 2)

    def test_get_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning('my learning', 's1')
            result = store.get_learning(added['year'], added['seq'])
            self.assertEqual(result['content'], 'my learning')

    def test_remove_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning('remove me', 's1')
            removed = store.remove_learning(added['year'], added['seq'])
            self.assertIsNotNone(removed)
            self.assertIsNone(store.get_learning(added['year'], added['seq']))

    def test_search_learnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_learning('encoding workaround', 's1')
            store.add_learning('api quirk', 's1')
            results = store.list_learnings(search='encoding')
            self.assertEqual(len(results), 1)


class TestEmptyContent(unittest.TestCase):
    def test_empty_content_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            year = datetime.now(timezone.utc).year
            store = ScribeStore(Path(tmp))
            result = store.add_note('todo', '', 's1')
            self.assertFalse(result['has_body'])
            md = Path(tmp) / 'todos' / str(year) / '1.md'
            self.assertEqual(md.read_text(encoding='utf-8'), '')


if __name__ == '__main__':
    unittest.main()
