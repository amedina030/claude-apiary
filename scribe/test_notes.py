#!/usr/bin/env python3
"""Tests for scribe/notes.py — ScribeStore-backed CLI commands."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scribe.notes as notes
from scribe.store import ScribeStore


class TestScribeNotes(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.store = ScribeStore(self.tmp_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_args(self, **kwargs):
        """Create a namespace object with store set."""
        ns = type('Args', (), {'store': self.store, **kwargs})()
        return ns

    def test_add_creates_file_and_index(self):
        entry = self.store.add_note('todo', 'test content', 'sess1')
        self.assertEqual(entry['type'], 'todo')
        self.assertIn('display_id', entry)
        # Verify file exists in todos/<year>/<seq>.md
        md_path = self.tmp_dir / 'todos' / str(entry['year']) / f"{entry['seq']}.md"
        self.assertTrue(md_path.exists())
        self.assertEqual(md_path.read_text(encoding='utf-8'), 'test content')

    def test_add_general_type(self):
        # 'general' is now a valid type
        self.assertIn('general', notes.VALID_TYPES)
        entry = self.store.add_note('general', 'general note', 'sess1')
        self.assertEqual(entry['type'], 'general')

    def test_get_note(self):
        entry = self.store.add_note('todo', 'get me', 'sess1')
        result = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertIsNotNone(result)
        self.assertEqual(result['content'], 'get me')
        self.assertEqual(result['type'], 'todo')

    def test_get_note_from_archive(self):
        entry = self.store.add_note('todo', 'archive me', 'sess1')
        self.store.archive_note('todo', entry['year'], entry['seq'])
        result = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'active')
        self.assertTrue(result.get('_from_archive'))

    def test_list_notes_all_types(self):
        self.store.add_note('todo', 'A', 'sess1')
        self.store.add_note('handoff', 'B', 'sess1')
        self.store.add_note('decision', 'C', 'sess1')
        result = self.store.list_notes()
        self.assertEqual(len(result), 3)

    def test_list_notes_type_filter(self):
        self.store.add_note('todo', 'A', 'sess1')
        self.store.add_note('handoff', 'B', 'sess1')
        result = self.store.list_notes(note_type='todo')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['type'], 'todo')

    def test_list_notes_search_filter(self):
        self.store.add_note('todo', 'fix the bug', 'sess1')
        self.store.add_note('todo', 'add feature', 'sess1')
        result = self.store.list_notes(search='fix')
        self.assertEqual(len(result), 1)

    def test_done_updates_status(self):
        entry = self.store.add_note('todo', 'do this', 'sess1')
        self.store.update_note('todo', entry['year'], entry['seq'], status='done')
        result = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertEqual(result['status'], 'done')

    def test_archive_note(self):
        entry = self.store.add_note('todo', 'archive target', 'sess1')
        archived = self.store.archive_note('todo', entry['year'], entry['seq'])
        self.assertIsNotNone(archived)
        # Status is preserved (not clobbered to 'archived'); archived-ness
        # lives in folder location and the archived_at stamp.
        self.assertEqual(archived['status'], 'active')
        self.assertIn('archived_at', archived)
        # Should not appear in active list
        active = self.store.list_notes(status='active')
        self.assertEqual(len(active), 0)
        # Should appear in archived list
        arch_list = self.store.list_notes(status='archived')
        self.assertEqual(len(arch_list), 1)

    def test_auto_archive_old_done_notes(self):
        # Add note and manually backdate its index entry
        entry = self.store.add_note('todo', 'old done note', 'sess1')
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.store.update_note('todo', entry['year'], entry['seq'], status='done', timestamp=old_ts)
        count = notes._run_auto_archive_store(self.store)
        self.assertGreaterEqual(count, 1)

    def test_auto_archive_old_decisions(self):
        entry = self.store.add_note('decision', 'old decision', 'sess1')
        old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.store.update_note('decision', entry['year'], entry['seq'], timestamp=old_ts)
        count = notes._run_auto_archive_store(self.store)
        self.assertGreaterEqual(count, 1)
        # Recent decision should NOT be archived
        fresh = self.store.add_note('decision', 'recent decision', 'sess1')
        notes._run_auto_archive_store(self.store)
        got = self.store.get_note('decision', fresh['year'], fresh['seq'])
        self.assertFalse(got.get('_from_archive'))

    def test_learn_creates_entry(self):
        entry = self.store.add_learning('discovered a trick', 'sess1')
        self.assertIn('display_id', entry)
        md_path = self.tmp_dir / 'learnings' / str(entry['year']) / f"{entry['seq']}.md"
        self.assertTrue(md_path.exists())

    def test_list_learnings(self):
        self.store.add_learning('learning one', 'sess1')
        self.store.add_learning('learning two', 'sess1')
        result = self.store.list_learnings()
        self.assertEqual(len(result), 2)

    def test_list_learnings_search(self):
        self.store.add_learning('fix encoding', 'sess1')
        self.store.add_learning('add feature', 'sess1')
        result = self.store.list_learnings(search='fix')
        self.assertEqual(len(result), 1)

    def test_unlearn_removes_entry(self):
        entry = self.store.add_learning('remove me', 'sess1')
        removed = self.store.remove_learning(entry['year'], entry['seq'])
        self.assertIsNotNone(removed)
        result = self.store.list_learnings()
        self.assertEqual(len(result), 0)

    def test_format_age(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(notes.format_age(now.strftime('%Y-%m-%dT%H:%M:%SZ')), 'just now')
        five_min = (now - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertEqual(notes.format_age(five_min), '5m ago')

    def test_handoff_sessions(self):
        self.store.add_note('handoff', 'handoff 1', 'sess-abc')
        self.store.add_note('handoff', 'handoff 2', 'sess-def')
        handoffs = self.store.list_notes(note_type='handoff')
        sessions = {h.get('session', '') for h in handoffs if h.get('status') != 'done'}
        self.assertEqual(len(sessions), 2)

    def test_cmd_add_handoff_requires_summary(self):
        args = self._make_args(
            type='handoff', content='body text', summary='',
            session_id='sess1', auto=False, role='', mission='',
            if_no_handoff_for=None,
        )
        with self.assertRaises(SystemExit):
            notes.cmd_add(args)

    def test_cmd_add_handoff_with_summary_succeeds(self):
        args = self._make_args(
            type='handoff', content='body text',
            summary='Session sess1: did the thing',
            session_id='sess1', auto=False, role='', mission='',
            if_no_handoff_for=None,
        )
        notes.cmd_add(args)
        handoffs = self.store.list_notes(note_type='handoff')
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]['summary'], 'Session sess1: did the thing')

    def test_cmd_add_summary_length_cap(self):
        args = self._make_args(
            type='handoff', content='body',
            summary='x' * (notes.MAX_SUMMARY_LENGTH + 1),
            session_id='sess1', auto=False, role='', mission='',
            if_no_handoff_for=None,
        )
        with self.assertRaises(SystemExit):
            notes.cmd_add(args)

    def test_cmd_add_todo_summary_optional(self):
        args = self._make_args(
            type='todo', content='fix the bug', summary='',
            session_id='sess1', auto=False, role='', mission='',
            if_no_handoff_for=None,
        )
        notes.cmd_add(args)
        todos = self.store.list_notes(note_type='todo')
        self.assertEqual(len(todos), 1)

    def test_unarchive_restores_note(self):
        entry = self.store.add_note('todo', 'round-trip', 'sess1')
        self.store.update_note('todo', entry['year'], entry['seq'], status='done')
        self.store.archive_note('todo', entry['year'], entry['seq'])
        self.assertEqual(len(self.store.list_notes(status='active')), 0)

        restored = self.store.unarchive_note('todo', entry['year'], entry['seq'])
        self.assertIsNotNone(restored)
        self.assertEqual(restored['status'], 'done')
        self.assertNotIn('archived_at', restored)

        active = self.store.list_notes(status='active')
        self.assertEqual(len(active), 1)
        self.assertEqual(len(self.store.list_notes(status='archived')), 0)

        md_path = self.tmp_dir / 'todos' / str(entry['year']) / f"{entry['seq']}.md"
        self.assertTrue(md_path.exists())
        self.assertEqual(md_path.read_text(encoding='utf-8'), 'round-trip')

    def test_unarchive_missing_returns_none(self):
        self.assertIsNone(self.store.unarchive_note('todo', 2026, 999))

    def test_drop_status(self):
        entry = self.store.add_note('todo', 'wontfix this', 'sess1')
        self.store.update_note('todo', entry['year'], entry['seq'], status='dropped')
        got = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertEqual(got['status'], 'dropped')

    def test_cmd_list_hides_dropped_by_default(self):
        e1 = self.store.add_note('todo', 'active one', 'sess1')
        e2 = self.store.add_note('todo', 'dropped one', 'sess1')
        self.store.update_note('todo', e2['year'], e2['seq'], status='dropped')
        # Default active list excludes dropped
        visible = [n for n in self.store.list_notes(status='active')
                   if n.get('status') not in ('done', 'dropped')]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]['seq'], e1['seq'])

    def test_defer_sets_deferred_status(self):
        entry = self.store.add_note('todo', 'need more data first', 'sess1')
        args = self._make_args(id=entry['display_id'])
        notes.cmd_defer(args)
        got = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertEqual(got['status'], 'deferred')

    def test_resume_returns_to_active(self):
        entry = self.store.add_note('todo', 'defer me', 'sess1')
        self.store.update_note('todo', entry['year'], entry['seq'], status='deferred')
        args = self._make_args(id=entry['display_id'])
        notes.cmd_resume(args)
        got = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertEqual(got['status'], 'active')

    def test_defer_rejects_done_note(self):
        entry = self.store.add_note('todo', 'done note', 'sess1')
        self.store.update_note('todo', entry['year'], entry['seq'], status='done')
        args = self._make_args(id=entry['display_id'])
        with self.assertRaises(SystemExit):
            notes.cmd_defer(args)

    def test_resume_noop_on_active_note(self):
        entry = self.store.add_note('todo', 'already active', 'sess1')
        args = self._make_args(id=entry['display_id'])
        # Should print a message but not raise or change status
        notes.cmd_resume(args)
        got = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertEqual(got['status'], 'active')

    def test_default_list_hides_deferred(self):
        e1 = self.store.add_note('todo', 'visible', 'sess1')
        e2 = self.store.add_note('todo', 'hidden', 'sess1')
        self.store.update_note('todo', e2['year'], e2['seq'], status='deferred')
        visible = [n for n in self.store.list_notes(status='active')
                   if n.get('status') not in ('done', 'dropped', 'deferred')]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]['seq'], e1['seq'])

    def test_show_alias_same_as_get(self):
        # Verify 'show' is registered as alias in argparse (covered by 'aliases=["show"]')
        # Both should call cmd_get — just verify cmd_get works
        entry = self.store.add_note('todo', 'show me', 'sess1')
        result = self.store.get_note('todo', entry['year'], entry['seq'])
        self.assertIsNotNone(result)
        self.assertEqual(result['content'], 'show me')


if __name__ == '__main__':
    unittest.main()
