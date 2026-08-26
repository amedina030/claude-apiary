#!/usr/bin/env python3
"""Tests for scribe/notes.py — ScribeStore-backed CLI commands."""
import json
import subprocess
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

    def test_cmd_add_content_file_reads_file_verbatim(self):
        raw = 'has `echo XXX` and /clear and filename.md'
        src = self.tmp_dir / 'content.md'
        src.write_text(raw, encoding='utf-8')
        args = self._make_args(
            type='context', content=None, content_file=str(src),
            summary='', session_id='sess1', auto=False, role='', mission='',
            if_no_handoff_for=None,
        )
        notes.cmd_add(args)
        stored = self.store.list_notes(note_type='context')
        self.assertEqual(len(stored), 1)
        full = self.store.get_note('context', stored[0]['year'], stored[0]['seq'])
        self.assertEqual(full['content'], raw)

    def test_cmd_add_content_file_missing_fails(self):
        args = self._make_args(
            type='context', content=None,
            content_file=str(self.tmp_dir / 'does_not_exist.md'),
            summary='', session_id='sess1', auto=False, role='', mission='',
            if_no_handoff_for=None,
        )
        with self.assertRaises(SystemExit):
            notes.cmd_add(args)

    def test_cmd_learn_content_file_reads_file_verbatim(self):
        raw = 'learned: `echo XXX` beats /clear on filename.md'
        src = self.tmp_dir / 'learning.md'
        src.write_text(raw, encoding='utf-8')
        args = self._make_args(
            content=None, content_file=str(src), session_id='sess1',
            brief_summary='', role='', mission='', tags='scribe', area=[],
            supersedes='',
        )
        notes.cmd_learn(args)
        learnings = self.store.list_learnings()
        self.assertEqual(len(learnings), 1)
        full = self.store.get_learning(learnings[0]['year'], learnings[0]['seq'])
        self.assertEqual(full['content'], raw)

    def test_cmd_learn_content_file_missing_fails(self):
        args = self._make_args(
            content=None, content_file=str(self.tmp_dir / 'nope.md'),
            session_id='sess1', brief_summary='', role='', mission='',
            tags='scribe', area=[], supersedes='',
        )
        with self.assertRaises(SystemExit):
            notes.cmd_learn(args)

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

    # ------------------------------------------------------------------
    # Template gate (T-2026-212)
    # ------------------------------------------------------------------

    def _write_template(self, note_type: str, body: str) -> str:
        path = notes.template_path(self.tmp_dir, note_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        return notes.template_hash(body)

    def _add_args(self, **overrides):
        defaults = dict(
            type='todo', content='body text', content_file=None,
            summary='', brief_summary='',
            session_id='sess1', auto=False, role='', mission='',
            if_no_handoff_for=None, ack_template='',
        )
        defaults.update(overrides)
        return self._make_args(**defaults)

    def test_template_gate_no_template_no_block(self):
        # No templates dir at all — add proceeds normally.
        notes.cmd_add(self._add_args())
        self.assertEqual(len(self.store.list_notes(note_type='todo')), 1)

    def test_template_gate_empty_file_no_block(self):
        # Whitespace-only template is treated as missing.
        self._write_template('todo', '   \n\n  ')
        notes.cmd_add(self._add_args())
        self.assertEqual(len(self.store.list_notes(note_type='todo')), 1)

    def test_template_gate_blocks_without_ack(self):
        self._write_template('todo', 'must include a target dir.')
        with self.assertRaises(SystemExit):
            notes.cmd_add(self._add_args())
        # Note must NOT be created when the gate trips.
        self.assertEqual(len(self.store.list_notes(note_type='todo')), 0)

    def test_template_gate_passes_with_correct_hash(self):
        h = self._write_template('todo', 'must include a target dir.')
        notes.cmd_add(self._add_args(ack_template=h))
        self.assertEqual(len(self.store.list_notes(note_type='todo')), 1)

    def test_template_gate_rejects_wrong_hash(self):
        self._write_template('todo', 'must include a target dir.')
        with self.assertRaises(SystemExit):
            notes.cmd_add(self._add_args(ack_template='deadbeefcafe'))
        self.assertEqual(len(self.store.list_notes(note_type='todo')), 0)

    def test_template_gate_only_affects_matching_type(self):
        # Template for 'todo' must not block adding a 'context' note.
        self._write_template('todo', 'todos need a target dir.')
        notes.cmd_add(self._add_args(type='context'))
        self.assertEqual(len(self.store.list_notes(note_type='context')), 1)

    def test_template_gate_detects_mid_flight_edit(self):
        h_old = self._write_template('todo', 'version 1')
        # User edits the template between Claude reading and writing.
        h_new = self._write_template('todo', 'version 2 — new rules')
        self.assertNotEqual(h_old, h_new)
        with self.assertRaises(SystemExit):
            notes.cmd_add(self._add_args(ack_template=h_old))
        self.assertEqual(len(self.store.list_notes(note_type='todo')), 0)
        # Re-acking with new hash succeeds.
        notes.cmd_add(self._add_args(ack_template=h_new))
        self.assertEqual(len(self.store.list_notes(note_type='todo')), 1)

    def test_template_path_helper(self):
        p = notes.template_path(self.tmp_dir, 'todo')
        self.assertEqual(p, self.tmp_dir / 'templates' / 'todo.md')

    def test_template_text_returns_none_when_missing(self):
        self.assertIsNone(notes.template_text(self.tmp_dir, 'todo'))

    def test_template_hash_deterministic(self):
        self.assertEqual(notes.template_hash('abc'), notes.template_hash('abc'))
        self.assertNotEqual(notes.template_hash('abc'), notes.template_hash('abcd'))
        self.assertEqual(len(notes.template_hash('abc')), notes.TEMPLATE_HASH_LEN)

    def test_cmd_template_show_prints_content(self):
        h = self._write_template('todo', 'use a target dir')
        args = self._make_args(template_action='show', type='todo')
        # Just verify it runs without raising; output goes to stdout.
        notes.cmd_template(args)

    def test_cmd_template_show_no_template_exits(self):
        args = self._make_args(template_action='show', type='todo')
        with self.assertRaises(SystemExit):
            notes.cmd_template(args)

    def test_cmd_template_path_runs(self):
        args = self._make_args(template_action='path', type='todo')
        notes.cmd_template(args)  # prints absolute path; no exit

    def test_cmd_template_list_runs(self):
        self._write_template('todo', 'one')
        self._write_template('context', 'two')
        args = self._make_args(template_action='list')
        notes.cmd_template(args)


class TestContentFileParserWiring(unittest.TestCase):
    """`--content-file` is the list-form-subprocess rule made real: callers with
    a multi-kilobyte body (an incubator spec, a /wrapup handoff) cannot put it on
    argv. Both `add` and `learn` must offer it, exclusively with `--content`."""

    NOTES_PY = Path(__file__).resolve().parent / 'notes.py'

    def _help(self, subcommand):
        return subprocess.run(
            [sys.executable, str(self.NOTES_PY), subcommand, '--help'],
            capture_output=True, text=True, encoding='utf-8',
        )

    def _both(self, subcommand):
        return subprocess.run(
            [sys.executable, str(self.NOTES_PY), subcommand,
             '--content', 'a', '--content-file', 'b'],
            capture_output=True, text=True, encoding='utf-8',
        )

    def test_content_file_is_offered_by_add_and_learn(self):
        for subcommand in ('add', 'learn'):
            with self.subTest(subcommand=subcommand):
                result = self._help(subcommand)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--content-file', result.stdout)

    def test_content_and_content_file_are_mutually_exclusive(self):
        for subcommand in ('add', 'learn'):
            with self.subTest(subcommand=subcommand):
                result = self._both(subcommand)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('not allowed with argument', result.stderr)


if __name__ == '__main__':
    unittest.main()
