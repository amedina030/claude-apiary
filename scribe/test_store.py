#!/usr/bin/env python3
"""Unit tests for scribe.store — folder-per-type storage engine."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe.store import ScribeStore, TYPE_FOLDERS, LEARNING_FOLDER, INDEX_FILENAME, ARCHIVE_DIRNAME, NEXT_ID_FILENAME


class TestEnsureLayout(unittest.TestCase):
    """AC: Given a fresh state_dir, ensure_layout creates all folders."""

    def test_creates_all_type_folders_with_index_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / 'scribe_state'
            store = ScribeStore(state_dir)
            all_folders = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]
            for folder_name in all_folders:
                folder = state_dir / folder_name
                self.assertTrue(folder.is_dir(), f"{folder_name} should exist")
                idx = folder / INDEX_FILENAME
                self.assertTrue(idx.exists(), f"{folder_name}/index.jsonl should exist")
                archive = folder / ARCHIVE_DIRNAME
                self.assertTrue(archive.is_dir(), f"{folder_name}/archive should exist")
                archive_idx = archive / INDEX_FILENAME
                self.assertTrue(archive_idx.exists())
            # next_id file
            nid = state_dir / NEXT_ID_FILENAME
            self.assertTrue(nid.exists())
            self.assertEqual(nid.read_text(encoding='utf-8').strip(), '1')


class TestAddNote(unittest.TestCase):
    """AC: add_note increments next_id, writes index entry and .md file."""

    def test_add_first_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            result = store.add_note('todo', 'fix bug', 'abc123')
            self.assertEqual(result['id'], 1)
            self.assertEqual(result['type'], 'todo')
            self.assertEqual(result['status'], 'active')
            self.assertEqual(result['session'], 'abc123')
            self.assertTrue(result['has_body'])
            # Check index file
            idx_path = Path(tmp) / 'todos' / INDEX_FILENAME
            lines = [l for l in idx_path.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry['id'], 1)
            # Check .md file
            md_path = Path(tmp) / 'todos' / '1.md'
            self.assertEqual(md_path.read_text(encoding='utf-8'), 'fix bug')
            # next_id should now be 2
            nid = Path(tmp) / NEXT_ID_FILENAME
            self.assertEqual(nid.read_text(encoding='utf-8').strip(), '2')

    def test_add_multiple_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            n1 = store.add_note('todo', 'first', 's1')
            n2 = store.add_note('decision', 'second', 's1')
            n3 = store.add_note('todo', 'third', 's1')
            self.assertEqual(n1['id'], 1)
            self.assertEqual(n2['id'], 2)
            self.assertEqual(n3['id'], 3)


class TestGetNote(unittest.TestCase):
    """AC: get_note returns index metadata and .md content."""

    def test_get_existing_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'fix bug', 's1')
            result = store.get_note(1)
            self.assertIsNotNone(result)
            self.assertEqual(result['id'], 1)
            self.assertEqual(result['content'], 'fix bug')

    def test_get_nonexistent_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            self.assertIsNone(store.get_note(999))

    def test_get_note_missing_body(self):
        """Index entry exists but .md file is gone -> content=None, warning."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'content here', 's1')
            # Delete the .md file
            md = Path(tmp) / 'todos' / '1.md'
            md.unlink()
            result = store.get_note(1)
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
            ids = [r['id'] for r in results]
            self.assertEqual(ids, [3, 2, 1])

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
    """AC: archive_note moves index entry and .md to archive/."""

    def test_archive_moves_entry_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'to archive', 's1')
            result = store.archive_note(1)
            self.assertIsNotNone(result)
            self.assertEqual(result['status'], 'archived')
            # Active index should be empty
            active = store._read_index(Path(tmp) / 'todos')
            self.assertEqual(len(active), 0)
            # Archive index should have the entry
            archived = store._read_index(Path(tmp) / 'todos' / ARCHIVE_DIRNAME)
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0]['id'], 1)
            # .md moved
            self.assertFalse((Path(tmp) / 'todos' / '1.md').exists())
            self.assertTrue((Path(tmp) / 'todos' / 'archive' / '1.md').exists())

    def test_get_archived_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'archive me', 's1')
            store.archive_note(1)
            result = store.get_note(1)
            self.assertIsNotNone(result)
            self.assertEqual(result['status'], 'archived')
            self.assertEqual(result['content'], 'archive me')


class TestConcurrency(unittest.TestCase):
    """AC: concurrent add_note calls get unique IDs without corruption."""

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
            ids = {r['id'] for r in results}
            self.assertEqual(len(ids), 10, "All IDs should be unique")


class TestNextIdRebuild(unittest.TestCase):
    """AC: next_id file deleted -> rebuild from max ID across indexes."""

    def test_rebuild_after_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'a', 's1')  # id=1
            store.add_note('decision', 'b', 's1')  # id=2
            store.add_note('todo', 'c', 's1')  # id=3
            # Delete next_id
            nid_path = Path(tmp) / NEXT_ID_FILENAME
            nid_path.unlink()
            # Next add should rebuild and get id=4
            result = store.add_note('todo', 'd', 's1')
            self.assertEqual(result['id'], 4)


class TestMalformedIndex(unittest.TestCase):
    """AC: malformed line in index.jsonl is skipped with warning."""

    def test_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'good note', 's1')
            # Inject a bad line
            idx = Path(tmp) / 'todos' / INDEX_FILENAME
            content = idx.read_text(encoding='utf-8')
            idx.write_text('NOT VALID JSON\n' + content, encoding='utf-8')
            # list_notes should still work, returning only the valid entry
            results = store.list_notes(note_type='todo')
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['id'], 1)


class TestUpdateNote(unittest.TestCase):
    def test_update_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'original', 's1')
            result = store.update_note(1, summary='updated summary')
            self.assertIsNotNone(result)
            self.assertEqual(result['summary'], 'updated summary')

    def test_update_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            store.add_note('todo', 'original', 's1')
            store.update_note(1, content='new body')
            md = Path(tmp) / 'todos' / '1.md'
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
            result = store.get_learning(added['id'])
            self.assertEqual(result['content'], 'my learning')

    def test_remove_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScribeStore(Path(tmp))
            added = store.add_learning('remove me', 's1')
            removed = store.remove_learning(added['id'])
            self.assertIsNotNone(removed)
            self.assertIsNone(store.get_learning(added['id']))

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
            store = ScribeStore(Path(tmp))
            result = store.add_note('todo', '', 's1')
            self.assertFalse(result['has_body'])
            md = Path(tmp) / 'todos' / '1.md'
            self.assertEqual(md.read_text(encoding='utf-8'), '')


if __name__ == '__main__':
    unittest.main()
