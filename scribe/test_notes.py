#!/usr/bin/env python3
"""Tests for scribe/notes.py."""
import json
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scribe.notes as notes


def _setup(tmp_dir):
    """Point notes module at temp files."""
    notes.NOTES_PATH = tmp_dir / "notes.jsonl"
    notes.ARCHIVE_PATH = tmp_dir / "notes_archive.jsonl"


def _add(tmp_dir, **kwargs):
    """Helper to add a note directly."""
    existing = notes._read_jsonl(notes.NOTES_PATH)
    note = {
        "id": notes._next_id(existing),
        "timestamp": kwargs.get("timestamp", notes._now_iso()),
        "session_id": kwargs.get("session_id", "test"),
        "type": kwargs.get("type", "todo"),
        "content": kwargs.get("content", "test note"),
        "status": kwargs.get("status", "active"),
        "auto_generated": kwargs.get("auto_generated", False),
    }
    notes._append_jsonl(notes.NOTES_PATH, note)
    return note


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_add_and_read(tmp_dir):
    _setup(tmp_dir)
    _add(tmp_dir, type="todo", content="Build tune.py")
    _add(tmp_dir, type="decision", content="Single-task inheritance only")
    result = notes._read_jsonl(notes.NOTES_PATH)
    assert len(result) == 2
    assert result[0]["type"] == "todo"
    assert result[1]["type"] == "decision"
    assert result[0]["id"] == 1
    assert result[1]["id"] == 2


def test_next_id(tmp_dir):
    _setup(tmp_dir)
    assert notes._next_id([]) == 1
    assert notes._next_id([{"id": 5}, {"id": 3}]) == 6


def test_done(tmp_dir):
    _setup(tmp_dir)
    _add(tmp_dir, type="todo", content="Fix the bug")
    all_notes = notes._read_jsonl(notes.NOTES_PATH)
    assert all_notes[0]["status"] == "active"

    # Mark done
    for n in all_notes:
        if n["id"] == 1:
            n["status"] = "done"
    notes._write_jsonl(notes.NOTES_PATH, all_notes)

    result = notes._read_jsonl(notes.NOTES_PATH)
    assert result[0]["status"] == "done"


def test_update(tmp_dir):
    _setup(tmp_dir)
    _add(tmp_dir, type="todo", content="Original")
    all_notes = notes._read_jsonl(notes.NOTES_PATH)
    all_notes[0]["content"] = "Updated content"
    notes._write_jsonl(notes.NOTES_PATH, all_notes)

    result = notes._read_jsonl(notes.NOTES_PATH)
    assert result[0]["content"] == "Updated content"


def test_type_filter(tmp_dir):
    _setup(tmp_dir)
    _add(tmp_dir, type="todo", content="A")
    _add(tmp_dir, type="handoff", content="B")
    _add(tmp_dir, type="todo", content="C")

    all_notes = notes._read_jsonl(notes.NOTES_PATH)
    todos = [n for n in all_notes if n["type"] == "todo"]
    assert len(todos) == 2
    handoffs = [n for n in all_notes if n["type"] == "handoff"]
    assert len(handoffs) == 1


def test_session_filter(tmp_dir):
    _setup(tmp_dir)
    _add(tmp_dir, session_id="abc123", content="Session A")
    _add(tmp_dir, session_id="def456", content="Session B")
    _add(tmp_dir, session_id="abc999", content="Session A2")

    all_notes = notes._read_jsonl(notes.NOTES_PATH)
    filtered = [n for n in all_notes if n["session_id"].startswith("abc")]
    assert len(filtered) == 2


def test_search_filter(tmp_dir):
    _setup(tmp_dir)
    _add(tmp_dir, content="Build tune.py v2")
    _add(tmp_dir, content="Fix the warning system")
    _add(tmp_dir, content="Tune the weights")

    all_notes = notes._read_jsonl(notes.NOTES_PATH)
    matches = [n for n in all_notes if "tune" in n["content"].lower()]
    assert len(matches) == 2


def test_format_age(tmp_dir):
    now = datetime.now(timezone.utc)
    assert notes._format_age(now.strftime("%Y-%m-%dT%H:%M:%SZ")) == "just now"

    five_min = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "5m ago" == notes._format_age(five_min)

    three_hours = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "3h ago" == notes._format_age(three_hours)

    two_days = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "2d ago" == notes._format_age(two_days)

    two_weeks = (now - timedelta(weeks=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "2w ago" == notes._format_age(two_weeks)


def test_auto_archive(tmp_dir):
    _setup(tmp_dir)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_ts = notes._now_iso()

    _add(tmp_dir, type="todo", content="Old done", status="done", timestamp=old_ts)
    _add(tmp_dir, type="handoff", content="Old handoff", timestamp=old_ts)
    _add(tmp_dir, type="todo", content="Recent todo", timestamp=recent_ts)

    all_notes = notes._read_jsonl(notes.NOTES_PATH)
    assert len(all_notes) == 3

    remaining, archived = notes._auto_archive(all_notes)
    assert len(remaining) == 1
    assert remaining[0]["content"] == "Recent todo"
    assert len(archived) == 2

    # Verify archive file
    archive = notes._read_jsonl(notes.ARCHIVE_PATH)
    assert len(archive) == 2


def test_archive_search(tmp_dir):
    _setup(tmp_dir)
    # Write directly to archive
    notes._append_jsonl(notes.ARCHIVE_PATH, {
        "id": 1, "type": "todo", "content": "Archived tune.py work",
        "status": "done", "timestamp": notes._now_iso(), "session_id": "old",
    })

    archive = notes._read_jsonl(notes.ARCHIVE_PATH)
    matches = [n for n in archive if "tune" in n["content"].lower()]
    assert len(matches) == 1


def test_migrate(tmp_dir):
    _setup(tmp_dir)
    # Create a fake notes.md
    md_path = tmp_dir / "notes.md"
    md_path.write_text(
        '1. [2026-03-15 16:49 UTC] [session: 57f858a4] this is a test note\n'
        '2. [2026-03-15 18:56 UTC] [session: unknown] HANDOFF — session summary here\n'
        '3. [2026-04-01 22:44 UTC] [session: 5d9ae400] TODO: Build something\n',
        encoding="utf-8",
    )

    notes.cmd_migrate(type("Args", (), {"path": str(md_path)})())

    result = notes._read_jsonl(notes.NOTES_PATH)
    assert len(result) == 3
    assert result[0]["session_id"] == "57f858a4"
    assert result[0]["type"] == "context"  # no recognized prefix
    assert result[1]["type"] == "handoff"
    assert result[2]["type"] == "todo"


def test_get_checks_archive(tmp_dir):
    _setup(tmp_dir)
    # Note only in archive
    notes._append_jsonl(notes.ARCHIVE_PATH, {
        "id": 99, "type": "decision", "content": "Archived decision",
        "status": "done", "timestamp": notes._now_iso(), "session_id": "old",
    })

    # Should find it in active first, then archive
    active = notes._read_jsonl(notes.NOTES_PATH)
    note = next((n for n in active if n.get("id") == 99), None)
    assert note is None  # not in active

    archive = notes._read_jsonl(notes.ARCHIVE_PATH)
    note = next((n for n in archive if n.get("id") == 99), None)
    assert note is not None
    assert note["content"] == "Archived decision"


def test_empty_file_returns_empty_list(tmp_dir):
    _setup(tmp_dir)
    assert notes._read_jsonl(notes.NOTES_PATH) == []
    assert notes._read_jsonl(notes.ARCHIVE_PATH) == []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)

        tests = [
            ("add and read", test_add_and_read),
            ("next_id", test_next_id),
            ("done", test_done),
            ("update", test_update),
            ("type filter", test_type_filter),
            ("session filter", test_session_filter),
            ("search filter", test_search_filter),
            ("format_age", test_format_age),
            ("auto_archive", test_auto_archive),
            ("archive search", test_archive_search),
            ("migrate", test_migrate),
            ("get checks archive", test_get_checks_archive),
            ("empty file returns empty", test_empty_file_returns_empty_list),
        ]

        for name, fn in tests:
            label = f"  {name:<36}"
            try:
                fn(Path(tempfile.mkdtemp(dir=td)))
                print(f"{label} OK")
            except Exception as e:
                print(f"{label} FAIL: {e}")
                raise

    print(f"\n{len(tests)}/{len(tests)} passed.")


if __name__ == "__main__":
    main()
