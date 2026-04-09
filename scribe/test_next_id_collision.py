#!/usr/bin/env python3
"""Tests for scribe's raw-file id scanner (todo #271).

Regression coverage for the collision discovered 2026-04-09: a handoff
record written without a trailing newline caused the next append to
concatenate onto the same line, producing two JSON objects on one line
that ``read_jsonl`` silently skipped. The subsequent write then read
``max=260`` from parseable records and allocated 261 again, colliding
with the invisible todo.

Fix: ``_next_id`` now consults ``_raw_max_id`` which scans raw file
bytes for ``"id":`` tokens so ids in malformed lines still reserve
their slot.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scribe.notes as notes


class RawMaxIdScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.file = self.tmp_path / "notes.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text: str):
        self.file.write_text(text, encoding="utf-8")

    def test_missing_file_returns_zero(self):
        self.assertEqual(notes._raw_max_id(self.file), 0)

    def test_empty_file_returns_zero(self):
        self._write("")
        self.assertEqual(notes._raw_max_id(self.file), 0)

    def test_single_line_extracted(self):
        self._write('{"id": 42, "type": "todo"}\n')
        self.assertEqual(notes._raw_max_id(self.file), 42)

    def test_multiple_lines_returns_max(self):
        self._write(
            '{"id": 5, "type": "todo"}\n'
            '{"id": 17, "type": "decision"}\n'
            '{"id": 3, "type": "context"}\n'
        )
        self.assertEqual(notes._raw_max_id(self.file), 17)

    def test_two_jsons_on_one_line_both_seen(self):
        """The exact 2026-04-09 failure mode: concatenated JSON objects."""
        self._write(
            '{"id": 5, "type": "todo"}\n'
            '{"id": 260, "type": "handoff"}{"id": 261, "type": "todo"}\n'
        )
        self.assertEqual(notes._raw_max_id(self.file), 261)

    def test_partial_or_malformed_line_still_yields_id(self):
        """Truncated JSON (missing closing brace) still exposes its id."""
        self._write('{"id": 99, "type": "todo", "content": "truncat')
        self.assertEqual(notes._raw_max_id(self.file), 99)

    def test_content_mentioning_id_does_not_match(self):
        """Escaped-quote content like ``reminder: id 5`` must not match.

        Inside a JSON string, literal ``"id":`` appears as ``\\"id\\":``
        and the regex's negative lookbehind keeps it out of the scan.
        """
        payload = json.dumps({"id": 3, "content": '{"id": 5}'})
        self._write(payload + "\n")
        # Should see 3 (the real id), not 5 (embedded in content).
        self.assertEqual(notes._raw_max_id(self.file), 3)

    def test_no_space_after_colon(self):
        self._write('{"id":42}\n')
        self.assertEqual(notes._raw_max_id(self.file), 42)

    def test_extra_whitespace_between_key_and_colon(self):
        self._write('{"id"   :  42}\n')
        self.assertEqual(notes._raw_max_id(self.file), 42)


class NextIdCollisionRegressionTests(unittest.TestCase):
    """End-to-end: _next_id must return a value strictly greater than
    every id in the file, including ids that live in malformed lines.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.file = self.tmp_path / "notes.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_collision_from_line_52_repair_scenario(self):
        """Reproduces the 2026-04-09 collision failure mode.

        Line 1: a valid record with id 5.
        Line 2: two concatenated objects on one line — ids 260 and 261.
                read_jsonl silently skips this line; parsed_max sees 5.
        Without the raw scan, _next_id would return 6 and collide. With
        the raw scan in place, _next_id must return 262.
        """
        self.file.write_text(
            '{"id": 5, "type": "todo"}\n'
            '{"id": 260, "type": "handoff"}{"id": 261, "type": "todo"}\n',
            encoding="utf-8",
        )
        parsed = notes.read_jsonl(self.file)
        # Sanity: read_jsonl sees exactly one record (line 2 is skipped)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["id"], 5)

        nxt = notes._next_id(parsed, path=self.file)
        self.assertEqual(nxt, 262)

    def test_next_id_without_path_keeps_old_behavior(self):
        """Callers that don't pass path retain the parseable-only max."""
        entries = [{"id": 5}, {"id": 3}]
        self.assertEqual(notes._next_id(entries), 6)

    def test_next_id_with_missing_file(self):
        """path to a nonexistent file falls back to parsed entries only."""
        entries = [{"id": 7}]
        self.assertEqual(notes._next_id(entries, path=self.file), 8)

    def test_next_id_with_empty_entries_and_nonempty_file(self):
        """Raw scan alone must carry the full next-id decision."""
        self.file.write_text('{"id": 42}\n', encoding="utf-8")
        self.assertEqual(notes._next_id([], path=self.file), 43)

    def test_next_id_raw_exceeds_parsed(self):
        """When raw scan finds a higher id than any parsed entry, win."""
        self.file.write_text(
            '{"id": 10}\n'
            '{"id": 100}{"id": 200}\n',  # malformed; skipped by parser
            encoding="utf-8",
        )
        parsed = notes.read_jsonl(self.file)
        nxt = notes._next_id(parsed, path=self.file)
        self.assertEqual(nxt, 201)


class MakeNoteCollisionSafetyTests(unittest.TestCase):
    """make_note must allocate ids safe against malformed-line collisions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.file = self.tmp_path / "notes.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_make_note_consults_raw_file(self):
        self.file.write_text(
            '{"id": 5, "type": "todo"}\n'
            '{"id": 260, "type": "handoff"}{"id": 261, "type": "todo"}\n',
            encoding="utf-8",
        )
        parsed = notes.read_jsonl(self.file)
        note = notes.make_note(
            parsed,
            type="todo",
            content="new note that must not collide",
            path=self.file,
        )
        self.assertEqual(note["id"], 262)

    def test_make_note_without_path_falls_back(self):
        """Backwards compat: omitting path keeps the old behavior."""
        parsed = [{"id": 5}]
        note = notes.make_note(
            parsed, type="todo", content="no path"
        )
        self.assertEqual(note["id"], 6)


class MakeLearningCollisionSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.file = self.tmp_path / "learnings.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_make_learning_consults_raw_file(self):
        self.file.write_text(
            '{"id": 1}\n'
            '{"id": 50}{"id": 51}\n',
            encoding="utf-8",
        )
        parsed = notes.read_jsonl(self.file)
        learning = notes.make_learning(
            parsed, content="new learning", path=self.file
        )
        self.assertEqual(learning["id"], 52)


if __name__ == "__main__":
    unittest.main()
