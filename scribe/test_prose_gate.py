"""The prose gate on ``notes.py add`` and ``learn``: advisory by default, blocking behind the flag, --force tags."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prose import gate as prose_gate
from scribe import notes as notes_mod
from scribe import templates as templates_mod
from scribe.store import ScribeStore

TELL = "To be fair, the cache was the problem. It's worth noting the fix was small."
CLEAN = "The cache was the problem. The fix was small."


def _add_args(store, **over):
    base = dict(
        store=store,
        content=None,
        content_file=None,
        type="todo",
        summary="",
        brief_summary="",
        session_id="s",
        auto=False,
        if_no_handoff_for=None,
        role="",
        mission="",
        tags="",
        unique_tag="",
        force=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _learn_args(store, **over):
    base = dict(
        store=store,
        content=None,
        content_file=None,
        session_id="s",
        brief_summary="",
        role="",
        mission="",
        tags="git",
        area=[],
        supersedes="",
        infer=False,
        no_infer=True,
        force=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class ProseGateOnAddTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.store = ScribeStore(Path(self._td.name))
        templates_mod.scaffold_defaults(self.store.state_dir)

    def add(self, content, blocking, **over):
        buf = io.StringIO()
        with (
            redirect_stderr(buf),
            mock.patch.object(prose_gate, "blocking_enabled", lambda: blocking),
        ):
            notes_mod.cmd_add(_add_args(self.store, content=content, **over))
        return buf.getvalue()

    def test_clean_content_is_silent(self):
        err = self.add(CLEAN, blocking=True)
        self.assertEqual(err, "")
        self.assertEqual(len(self.store.list_notes(note_type="todo")), 1)

    def test_advisory_when_flag_off(self):
        err = self.add(TELL, blocking=False)
        self.assertIn("CandourPhrases", err)
        self.assertIn("Advisory", err)
        notes = self.store.list_notes(note_type="todo")
        self.assertEqual(len(notes), 1)
        self.assertNotIn(prose_gate.FORCED_TAG, notes[0].get("tags", []))

    def test_error_blocks_when_flag_on_and_writes_nothing(self):
        buf = io.StringIO()
        with (
            redirect_stderr(buf),
            mock.patch.object(prose_gate, "blocking_enabled", lambda: True),
            self.assertRaises(SystemExit),
        ):
            notes_mod.cmd_add(_add_args(self.store, content=TELL))
        self.assertIn("--force", buf.getvalue())
        self.assertEqual(len(self.store.list_notes(note_type="todo", status="all")), 0)

    def test_force_adds_and_tags(self):
        err = self.add(TELL, blocking=True, force=True)
        self.assertIn("bypassed via --force", err)
        notes = self.store.list_notes(note_type="todo")
        self.assertEqual(len(notes), 1)
        self.assertIn(prose_gate.FORCED_TAG, notes[0].get("tags", []))

    def test_warnings_never_block(self):
        err = self.add("In summary, the cache was the problem.", blocking=True)
        self.assertIn("SummaryCloser", err)
        self.assertEqual(len(self.store.list_notes(note_type="todo")), 1)

    def test_gate_failure_never_loses_a_note(self):
        with mock.patch.object(prose_gate, "gate", side_effect=RuntimeError("boom")):
            self.add(TELL, blocking=True)
        self.assertEqual(len(self.store.list_notes(note_type="todo")), 1)


class ProseGateOnLearnTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.store = ScribeStore(Path(self._td.name))

    def test_error_blocks_learn_when_flag_on(self):
        buf = io.StringIO()
        with (
            redirect_stderr(buf),
            mock.patch.object(prose_gate, "blocking_enabled", lambda: True),
            self.assertRaises(SystemExit),
        ):
            notes_mod.cmd_learn(_learn_args(self.store, content=TELL))
        self.assertEqual(self.store.list_learnings(), [])

    def test_force_learns_and_tags(self):
        buf = io.StringIO()
        with redirect_stderr(buf), mock.patch.object(prose_gate, "blocking_enabled", lambda: True):
            notes_mod.cmd_learn(_learn_args(self.store, content=TELL, force=True))
        learnings = self.store.list_learnings()
        self.assertEqual(len(learnings), 1)
        self.assertIn(prose_gate.FORCED_TAG, learnings[0].get("tags", []))
        self.assertIn("git", learnings[0].get("tags", []))


if __name__ == "__main__":
    unittest.main()
