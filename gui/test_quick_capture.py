"""Tests for the sidebar quick-capture bridge method (#T-2026-251).

The frontend half (auto-grow, Enter-to-save, the focus shortcut) is DOM-bound
and has no harness; what's covered here is the part with real logic — input
validation, target resolution, and the failure contract that keeps a typed
thought from being thrown away.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui import app as gui_app
from gui.app import GuiBridge
from scribe.store import ScribeStore


class _FakeSession:
    """Stands in for a Session: the bridge only needs cwd + session_id."""

    def __init__(self, cwd: Path, session_id: str = "abcdef1234567890"):
        self.cwd = cwd
        self.session_id = session_id
        self.flushed = 0

    def flush_notes(self):
        self.flushed += 1


class _FakeApp:
    def __init__(self, session=None):
        self.active = session


class QuickNoteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        # A ScribeStore on a temp dir, so writes are real but isolated.
        self.store = ScribeStore(self.root / "state" / "scribe")
        self.session = _FakeSession(self.repo)
        self.bridge = GuiBridge(_FakeApp(self.session))  # type: ignore[arg-type]

    def _with_store(self):
        return mock.patch.object(gui_app.scribe_api, "open_store", return_value=self.store)

    # --- happy path --------------------------------------------------------

    def test_note_is_written_and_id_returned(self):
        with self._with_store():
            res = self.bridge.add_quick_note("try the thing with the other thing")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["display_id"].startswith("W-"), res["display_id"])
        self.assertEqual(res["error"], "")
        notes = self.store.list_notes(status="active")
        self.assertEqual(len(notes), 1)
        self.assertIn("other thing", notes[0]["summary"])

    def test_default_type_is_wishlist(self):
        # A mid-run thought is not a commitment, and wishlist is one of the
        # types that actually surfaces in the startup banner.
        with self._with_store():
            self.bridge.add_quick_note("some idea")
        self.assertEqual(self.store.list_notes(status="active")[0]["type"], "wishlist")

    def test_todo_type_is_accepted(self):
        with self._with_store():
            res = self.bridge.add_quick_note("actually do this", "todo")
        self.assertTrue(res["ok"])
        self.assertTrue(res["display_id"].startswith("T-"))

    def test_session_id_is_recorded_for_provenance(self):
        with self._with_store():
            self.bridge.add_quick_note("thought")
        entry = self.store.list_notes(status="active")[0]
        self.assertEqual(entry["session"], self.session.session_id[:8])

    def test_store_is_opened_with_an_explicit_apiary_root(self):
        # scribe's own fallback derives main-apiary from __file__, which points
        # inside the bundle in a frozen build (the T-2026-248 failure). The GUI
        # must hand it the source/frozen-aware root instead.
        with mock.patch.object(gui_app.scribe_api, "open_store", return_value=self.store) as opener:
            self.bridge.add_quick_note("thought")
        self.assertIn("apiary_repo", opener.call_args.kwargs)
        self.assertIsNotNone(opener.call_args.kwargs["apiary_repo"])

    def test_sidebar_is_refreshed_so_the_note_shows_now(self):
        with self._with_store():
            self.bridge.add_quick_note("thought")
        self.assertEqual(self.session.flushed, 1)

    def test_text_is_stripped(self):
        with self._with_store():
            self.bridge.add_quick_note("   padded thought   ")
        self.assertEqual(self.store.list_notes(status="active")[0]["summary"], "padded thought")

    # --- refusals ----------------------------------------------------------

    def test_empty_text_is_refused(self):
        with self._with_store():
            res = self.bridge.add_quick_note("   \n  ")
        self.assertFalse(res["ok"])
        self.assertEqual(self.store.list_notes(status="active"), [])

    def test_non_string_is_refused(self):
        with self._with_store():
            res = self.bridge.add_quick_note(None)  # type: ignore[arg-type]
        self.assertFalse(res["ok"])

    def test_unsupported_type_is_refused(self):
        # The box is a low-friction inbox, not a general note editor — the
        # frontend must not be able to write arbitrary types.
        with self._with_store():
            res = self.bridge.add_quick_note("x", "handoff")
        self.assertFalse(res["ok"])
        self.assertIn("unsupported", res["error"])
        self.assertEqual(self.store.list_notes(status="active"), [])

    def test_no_active_tab_is_refused(self):
        bridge = GuiBridge(_FakeApp(None))  # type: ignore[arg-type]
        with self._with_store():
            res = bridge.add_quick_note("thought")
        self.assertFalse(res["ok"])
        self.assertIn("no active tab", res["error"])

    # --- failure contract --------------------------------------------------

    def test_unregistered_repo_reports_cleanly(self):
        # The folder picker opens ANY directory, so a tab whose cwd isn't a
        # registered apiary repo is an ordinary case, not an edge one. It must
        # surface as an error the frontend can show, never as a crash.
        with mock.patch.object(
            gui_app.scribe_api, "open_store", side_effect=KeyError("target repo not registered")
        ):
            res = self.bridge.add_quick_note("thought")
        self.assertFalse(res["ok"])
        self.assertIn("KeyError", res["error"])

    def test_write_failure_is_reported_not_raised(self):
        with mock.patch.object(gui_app.scribe_api, "open_store", side_effect=OSError("disk gone")):
            res = self.bridge.add_quick_note("thought")
        self.assertFalse(res["ok"])
        self.assertIn("disk gone", res["error"])
        self.assertEqual(res["display_id"], "")

    def test_flush_failure_does_not_lose_the_note(self):
        # The note is already committed to the store by the time we refresh;
        # a failed refresh must not be reported as a failed save.
        self.session.flush_notes = mock.Mock(side_effect=RuntimeError("bridge down"))
        with self._with_store():
            res = self.bridge.add_quick_note("thought")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(self.store.list_notes(status="active")), 1)


if __name__ == "__main__":
    unittest.main()
