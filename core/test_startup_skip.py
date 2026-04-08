"""Tests for backfill_skip.json loader and get_unseen_sessions integration."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import startup


class TestLoadSkipPrefixes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name) / "backfill_skip.json"

    def tearDown(self):
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

        # Handoff notes: only cccccccc has a handoff
        self._notes_patch = patch.object(
            startup, "read_jsonl",
            return_value=[{
                "type": "handoff",
                "status": "open",
                "session_id": "cccccccc-1111-2222-3333-444444444444",
            }],
        )
        self._notes_path_patch = patch.object(
            startup, "notes_path", return_value=self.tmp_dir / "notes.jsonl"
        )
        self._notes_patch.start()
        self._notes_path_patch.start()

    def tearDown(self):
        self._notes_patch.stop()
        self._notes_path_patch.stop()
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

        self._notes_path_patch = patch.object(
            startup, "notes_path", return_value=self.notes_file
        )
        self._archive_path_patch = patch.object(
            startup, "archive_path", return_value=self.archive_file
        )
        self._notes_path_patch.start()
        self._archive_path_patch.start()

    def tearDown(self):
        self._notes_path_patch.stop()
        self._archive_path_patch.stop()
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
        # aaaaaaaa's handoff lives only in the archive, not active notes
        self._write_jsonl(self.notes_file, [])
        self._write_jsonl(self.archive_file, [{
            "type": "handoff",
            "status": "active",
            "session_id": "aaaaaaaa-1111-2222-3333-444444444444",
        }])
        result = self._run()
        self.assertEqual(self._sids(result), {"bbbbbbbb"})

    def test_active_and_archived_handoffs_both_recognized(self):
        self._write_jsonl(self.notes_file, [{
            "type": "handoff",
            "status": "active",
            "session_id": "bbbbbbbb-1111-2222-3333-444444444444",
        }])
        self._write_jsonl(self.archive_file, [{
            "type": "handoff",
            "status": "active",
            "session_id": "aaaaaaaa-1111-2222-3333-444444444444",
        }])
        result = self._run()
        self.assertEqual(self._sids(result), set())

    def test_missing_archive_file_does_not_crash(self):
        self._write_jsonl(self.notes_file, [{
            "type": "handoff",
            "status": "active",
            "session_id": "aaaaaaaa-1111-2222-3333-444444444444",
        }])
        # archive_file deliberately not created
        result = self._run()
        self.assertEqual(self._sids(result), {"bbbbbbbb"})


if __name__ == "__main__":
    unittest.main()
