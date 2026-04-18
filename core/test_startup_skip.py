"""Tests for backfill_skip.json loader and get_unseen_sessions integration.

All tests in this file exercise the legacy per-project state layout. Each
test class pins ``APIARY_STATE_LAYOUT=legacy`` at setUp time so the tests
continue to cover that fallback even though the module default flipped
to the in-repo layout in todo #268. Repo-layout behavior has its own tests
in core/test_startup_repo_layout.py.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import startup
import scribe.notes as scribe_notes


def _force_legacy_layout():
    """Return a started mock.patch.dict handle pinning the legacy layout."""
    handle = patch.dict(
        os.environ, {scribe_notes.STATE_LAYOUT_ENV: "legacy"}
    )
    handle.start()
    return handle


class _FakeStore:
    """Drop-in ScribeStore stub: list_notes returns a preset list."""

    def __init__(self, handoffs):
        self._handoffs = list(handoffs)

    def list_notes(self, note_type=None, status=None):
        return [
            h for h in self._handoffs
            if note_type is None or h.get("type") == note_type
        ]


class TestLoadSkipPrefixes(unittest.TestCase):
    def setUp(self):
        self._layout_patch = _force_legacy_layout()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name) / "backfill_skip.json"

    def tearDown(self):
        self._layout_patch.stop()
        self._tmp.cleanup()

    def _write(self, content):
        self.tmp_path.write_text(content, encoding="utf-8")

    def _run(self):
        with patch.object(startup, "BACKFILL_SKIP_PATH", self.tmp_path):
            return startup.load_skip_prefixes()

    def test_missing_file_returns_empty_set(self):
        self.assertEqual(self._run(), set())

    def test_malformed_json_returns_empty_set(self):
        self._write("{not json")
        self.assertEqual(self._run(), set())

    def test_empty_file_returns_empty_set(self):
        self._write("")
        self.assertEqual(self._run(), set())

    def test_missing_skipped_key_returns_empty_set(self):
        self._write("{}")
        self.assertEqual(self._run(), set())

    def test_skipped_not_list_returns_empty_set(self):
        self._write(json.dumps({"skipped": "foo"}))
        self.assertEqual(self._run(), set())

    def test_top_level_not_dict_returns_empty_set(self):
        self._write(json.dumps(["list", "at", "top"]))
        self.assertEqual(self._run(), set())

    def test_entry_missing_session_id(self):
        self._write(json.dumps({
            "skipped": [
                {"reason": "x"},
                {"session_id": "abcdef12-1111-2222-3333-444444444444"},
            ]
        }))
        self.assertEqual(self._run(), {"abcdef12"})

    def test_entry_empty_session_id(self):
        self._write(json.dumps({
            "skipped": [
                {"session_id": ""},
                {"session_id": "12345678-1111-2222-3333-444444444444"},
            ]
        }))
        self.assertEqual(self._run(), {"12345678"})

    def test_uppercase_normalized(self):
        self._write(json.dumps({
            "skipped": [{"session_id": "ABCDEF12-AAAA-BBBB-CCCC-DDDDDDDDDDDD"}]
        }))
        self.assertEqual(self._run(), {"abcdef12"})

    def test_full_uuid_normalized(self):
        self._write(json.dumps({
            "skipped": [{"session_id": "abcdef12-3456-7890-abcd-ef1234567890"}]
        }))
        self.assertEqual(self._run(), {"abcdef12"})

    def test_multiple_entries(self):
        self._write(json.dumps({
            "skipped": [
                {"session_id": "aaaaaaaa-1111-2222-3333-444444444444"},
                {"session_id": "bbbbbbbb-1111-2222-3333-444444444444"},
            ]
        }))
        self.assertEqual(self._run(), {"aaaaaaaa", "bbbbbbbb"})

    def test_non_dict_entry_skipped(self):
        self._write(json.dumps({
            "skipped": [
                "not a dict",
                {"session_id": "cccccccc-1111-2222-3333-444444444444"},
            ]
        }))
        self.assertEqual(self._run(), {"cccccccc"})


class TestGetUnseenSessionsSkipIntegration(unittest.TestCase):
    def setUp(self):
        self._layout_patch = _force_legacy_layout()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.history_path = self.tmp_dir / ".session-history.json"
        self.skip_path = self.tmp_dir / "backfill_skip.json"

        history = [
            {
                "session_id": "aaaaaaaa-1111-2222-3333-444444444444",
                "transcript_path": "/t/aaa.jsonl",
                "role": "user",
                "mission": "general",
            },
            {
                "session_id": "bbbbbbbb-1111-2222-3333-444444444444",
                "transcript_path": "/t/bbb.jsonl",
                "role": "user",
                "mission": "general",
            },
            {
                "session_id": "cccccccc-1111-2222-3333-444444444444",
                "transcript_path": "/t/ccc.jsonl",
                "role": "user",
                "mission": "general",
            },
        ]
        self.history_path.write_text(json.dumps(history), encoding="utf-8")

        # Handoff notes: only cccccccc has a handoff.
        # Patch ScribeStore so list_notes returns the single seeded handoff.
        self._store_patch = patch.object(
            startup, "ScribeStore",
            return_value=_FakeStore([{
                "type": "handoff",
                "status": "open",
                "session": "cccccccc-1111-2222-3333-444444444444",
            }]),
        )
        self._store_patch.start()

    def tearDown(self):
        self._store_patch.stop()
        self._layout_patch.stop()
        self._tmp.cleanup()

    def _run(self):
        current = "99999999-1111-2222-3333-444444444444"
        with patch.object(startup, "HISTORY_PATH", self.history_path), \
             patch.object(startup, "BACKFILL_SKIP_PATH", self.skip_path):
            return startup.get_unseen_sessions(current, "user", "general", "claude-apiary")

    def _sids(self, unseen):
        return {s["session_id"][:8] for s in unseen}

    def test_skipped_session_excluded(self):
        self.skip_path.write_text(json.dumps({
            "skipped": [
                {"session_id": "aaaaaaaa-1111-2222-3333-444444444444",
                 "reason": "x", "date": "2026-04-07"},
            ]
        }), encoding="utf-8")
        result = self._run()
        self.assertEqual(self._sids(result), {"bbbbbbbb"})

    def test_missing_skip_file_proceeds(self):
        # skip_path does not exist
        result = self._run()
        self.assertEqual(self._sids(result), {"aaaaaaaa", "bbbbbbbb"})

    def test_malformed_skip_file_no_exception(self):
        self.skip_path.write_text("{not json", encoding="utf-8")
        result = self._run()
        self.assertEqual(self._sids(result), {"aaaaaaaa", "bbbbbbbb"})

    def test_skip_and_handoff_overlap(self):
        # cccccccc is in both the handoff notes and the skip file
        self.skip_path.write_text(json.dumps({
            "skipped": [
                {"session_id": "cccccccc-1111-2222-3333-444444444444",
                 "reason": "x", "date": "2026-04-07"},
            ]
        }), encoding="utf-8")
        result = self._run()
        # cccccccc excluded exactly once, not doubly-filtered or errored
        self.assertEqual(self._sids(result), {"aaaaaaaa", "bbbbbbbb"})


class TestGetUnseenSessionsArchiveIntegration(unittest.TestCase):
    """Handoffs that have been moved to notes_archive.jsonl must still
    count as 'seen' — otherwise archived sessions reappear forever."""

    def setUp(self):
        self._layout_patch = _force_legacy_layout()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.history_path = self.tmp_dir / ".session-history.json"
        self.skip_path = self.tmp_dir / "backfill_skip.json"
        self.notes_file = self.tmp_dir / "notes.jsonl"
        self.archive_file = self.tmp_dir / "notes_archive.jsonl"

        history = [
            {"session_id": "aaaaaaaa-1111-2222-3333-444444444444",
             "transcript_path": "/t/a.jsonl", "role": "user", "mission": "general"},
            {"session_id": "bbbbbbbb-1111-2222-3333-444444444444",
             "transcript_path": "/t/b.jsonl", "role": "user", "mission": "general"},
        ]
        self.history_path.write_text(json.dumps(history), encoding="utf-8")

        # Subclasses populate self._handoffs before calling _install_store_patch().
        self._handoffs: list[dict] = []
        self._store_patch = None

    def _install_store_patch(self):
        self._store_patch = patch.object(
            startup, "ScribeStore", return_value=_FakeStore(self._handoffs)
        )
        self._store_patch.start()

    def tearDown(self):
        if self._store_patch is not None:
            self._store_patch.stop()
        self._layout_patch.stop()
        self._tmp.cleanup()

    def _write_jsonl(self, path, entries):
        path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n",
            encoding="utf-8",
        )

    def _run(self):
        current = "99999999-1111-2222-3333-444444444444"
        with patch.object(startup, "HISTORY_PATH", self.history_path), \
             patch.object(startup, "BACKFILL_SKIP_PATH", self.skip_path):
            return startup.get_unseen_sessions(current, "user", "general", "claude-apiary")

    def _sids(self, unseen):
        return {s["session_id"][:8] for s in unseen}

    def test_archived_handoff_counts_as_seen(self):
        # aaaaaaaa's handoff lives only in the archive; store.list_notes(status="all")
        # returns both active and archived, so aaaaaaaa must still be filtered out.
        self._handoffs = [{
            "type": "handoff",
            "status": "archived",
            "session": "aaaaaaaa-1111-2222-3333-444444444444",
        }]
        self._install_store_patch()
        result = self._run()
        self.assertEqual(self._sids(result), {"bbbbbbbb"})

    def test_active_and_archived_handoffs_both_recognized(self):
        self._handoffs = [
            {"type": "handoff", "status": "active",
             "session": "bbbbbbbb-1111-2222-3333-444444444444"},
            {"type": "handoff", "status": "archived",
             "session": "aaaaaaaa-1111-2222-3333-444444444444"},
        ]
        self._install_store_patch()
        result = self._run()
        self.assertEqual(self._sids(result), set())

    def test_missing_archive_file_does_not_crash(self):
        # No archived handoffs present — only active. Exercises the pre-v2
        # "archive file absent" scenario via an empty store.
        self._handoffs = [{
            "type": "handoff",
            "status": "active",
            "session": "aaaaaaaa-1111-2222-3333-444444444444",
        }]
        self._install_store_patch()
        result = self._run()
        self.assertEqual(self._sids(result), {"bbbbbbbb"})


class TestRunSkip(unittest.TestCase):
    """Cover run_skip() — the writer that powers `startup.py skip`."""

    def setUp(self):
        self._layout_patch = _force_legacy_layout()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name) / "backfill_skip.json"

    def tearDown(self):
        self._layout_patch.stop()
        self._tmp.cleanup()

    def _run(self, session_id, reason="test", date=None):
        with patch.object(startup, "BACKFILL_SKIP_PATH", self.tmp_path):
            return startup.run_skip(session_id, reason, date=date)

    def _load(self):
        return json.loads(self.tmp_path.read_text(encoding="utf-8"))

    def test_creates_file_when_missing(self):
        status, path = self._run("aaaaaaaa-1111-2222-3333-444444444444", "startup-only")
        self.assertEqual(status, "added")
        self.assertEqual(path, self.tmp_path)
        self.assertTrue(self.tmp_path.exists())
        data = self._load()
        self.assertEqual(len(data["skipped"]), 1)
        entry = data["skipped"][0]
        self.assertEqual(entry["session_id"], "aaaaaaaa-1111-2222-3333-444444444444")
        self.assertEqual(entry["reason"], "startup-only")
        self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_idempotent_on_full_id(self):
        self._run("aaaaaaaa-1111-2222-3333-444444444444")
        status, _ = self._run("aaaaaaaa-1111-2222-3333-444444444444")
        self.assertEqual(status, "exists")
        self.assertEqual(len(self._load()["skipped"]), 1)

    def test_idempotent_on_prefix_match(self):
        """Different full UUIDs with the same 8-char prefix dedupe."""
        self._run("aaaaaaaa-1111-2222-3333-444444444444")
        status, _ = self._run("AAAAAAAA-9999-8888-7777-666666666666")
        self.assertEqual(status, "exists")
        self.assertEqual(len(self._load()["skipped"]), 1)

    def test_preserves_existing_entries(self):
        self.tmp_path.write_text(json.dumps({
            "skipped": [
                {"session_id": "cccccccc-1111-2222-3333-444444444444",
                 "reason": "pre-existing", "date": "2026-01-01"},
            ]
        }), encoding="utf-8")
        self._run("aaaaaaaa-1111-2222-3333-444444444444", "new")
        data = self._load()
        self.assertEqual(len(data["skipped"]), 2)
        self.assertEqual(data["skipped"][0]["reason"], "pre-existing")
        self.assertEqual(data["skipped"][1]["reason"], "new")

    def test_explicit_date_used(self):
        self._run("aaaaaaaa-1111-2222-3333-444444444444", date="2020-01-15")
        self.assertEqual(self._load()["skipped"][0]["date"], "2020-01-15")

    def test_empty_session_id_raises(self):
        with self.assertRaises(ValueError):
            self._run("")

    def test_malformed_file_overwritten(self):
        self.tmp_path.write_text("{not json", encoding="utf-8")
        status, _ = self._run("aaaaaaaa-1111-2222-3333-444444444444")
        self.assertEqual(status, "added")
        self.assertEqual(len(self._load()["skipped"]), 1)

    def test_creates_parent_dirs(self):
        nested = Path(self._tmp.name) / "a" / "b" / "backfill_skip.json"
        with patch.object(startup, "BACKFILL_SKIP_PATH", nested):
            startup.run_skip("aaaaaaaa-1111-2222-3333-444444444444", "x")
        self.assertTrue(nested.exists())


if __name__ == "__main__":
    unittest.main()
