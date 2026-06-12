"""Tests for the frozen public API surface (scribe/api.py, parity spec §6)."""
import json
import sys
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scribe import api


def _make_apiary(root: Path, entries: dict) -> None:
    """Write a synthetic .repos/registry.json under *root* (fake apiary repo).

    *entries* maps id_str -> {"name": ..., "real_path": ...}. Each entry's
    state folder (<root>/.repos/<name>-<id>) is created so find_state_dir and
    open_store have somewhere to land.
    """
    repos = root / ".repos"
    repos.mkdir(parents=True, exist_ok=True)
    (repos / "registry.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")
    for id_str, entry in entries.items():
        (repos / f"{entry['name']}-{id_str}").mkdir(parents=True, exist_ok=True)


class TestParseTs(unittest.TestCase):
    def test_plain_iso(self):
        dt = api.parse_ts("2026-06-12T13:44:59.018646+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_trailing_z(self):
        dt = api.parse_ts("2026-05-18T18:12:31.264Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_garbage_returns_none(self):
        self.assertIsNone(api.parse_ts("not a date"))
        self.assertIsNone(api.parse_ts(""))
        self.assertIsNone(api.parse_ts(None))


class TestDisplayId(unittest.TestCase):
    def test_round_trip_all_types(self):
        for note_type in api.TYPE_TO_PREFIX:
            did = api.format_display_id(note_type, 2026, 7)
            self.assertEqual(api.parse_display_id(did), (note_type, 2026, 7))

    def test_lowercase_prefix_parsed(self):
        self.assertEqual(api.parse_display_id("t-2026-1"), ("todo", 2026, 1))

    def test_unknown_prefix_raises(self):
        # K is the source-only ticket prefix; apiary has none.
        with self.assertRaises(ValueError):
            api.parse_display_id("K-2026-5")

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            api.parse_display_id("T-26-1")
        with self.assertRaises(ValueError):
            api.parse_display_id("garbage")

    def test_format_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            api.format_display_id("ticket", 2026, 1)


class TestResolver(unittest.TestCase):
    def test_resolve_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            apiary = Path(td)
            _make_apiary(apiary, {"7": {"name": "foo", "real_path": str(apiary / "src")}})
            got = api.resolve_scribe_dir("foo", apiary_repo=apiary)
            self.assertEqual(got, apiary / ".repos" / "foo-7" / "scribe")

    def test_resolve_unknown_name_raises_keyerror(self):
        with tempfile.TemporaryDirectory() as td:
            apiary = Path(td)
            _make_apiary(apiary, {"7": {"name": "foo", "real_path": str(apiary / "src")}})
            with self.assertRaises(KeyError):
                api.resolve_scribe_dir("nope", apiary_repo=apiary)

    def test_resolve_ambiguous_name_raises_valueerror(self):
        with tempfile.TemporaryDirectory() as td:
            apiary = Path(td)
            _make_apiary(apiary, {
                "3": {"name": "dup", "real_path": str(apiary / "a")},
                "4": {"name": "dup", "real_path": str(apiary / "b")},
            })
            with self.assertRaises(ValueError):
                api.resolve_scribe_dir("dup", apiary_repo=apiary)

    def test_resolve_by_repo_path_via_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            apiary = Path(td) / "apiary"
            _make_apiary(apiary, {"7": {"name": "foo", "real_path": "ignored"}})
            target = Path(td) / "target"
            pointer_dir = target / ".apiary"
            pointer_dir.mkdir(parents=True)
            (pointer_dir / "pointer").write_text(
                json.dumps({"apiary_repo": str(apiary), "target_id": "foo-7"}),
                encoding="utf-8",
            )
            got = api.resolve_scribe_dir(target, apiary_repo=apiary)
            self.assertEqual(got, apiary / ".repos" / "foo-7" / "scribe")

    def test_open_store_round_trips_a_note(self):
        with tempfile.TemporaryDirectory() as td:
            apiary = Path(td)
            _make_apiary(apiary, {"7": {"name": "foo", "real_path": str(apiary / "src")}})
            store = api.open_store("foo", apiary_repo=apiary)
            entry = store.add_note("todo", "body text", session_id="s1")
            fetched = store.get_note("todo", entry["year"], entry["seq"])
            self.assertEqual(fetched["content"], "body text")


class TestNormalizeEntry(unittest.TestCase):
    def test_synthesizes_stable_shape(self):
        norm = api.normalize_entry({
            "display_id": "T-2026-1", "type": "todo", "year": 2026, "seq": 1,
            "status": "active", "timestamp": "2026-06-12T00:00:00+00:00",
        })
        self.assertEqual(norm["tags"], [])
        self.assertEqual(norm["areas"], [])
        self.assertIs(norm["auto_generated"], False)
        self.assertIs(norm["archived"], False)
        self.assertIsNone(norm["status_changed_at"])

    def test_archived_from_archived_at(self):
        self.assertTrue(api.normalize_entry({"archived_at": "2026-06-12T00:00:00+00:00"})["archived"])

    def test_archived_from_flag(self):
        self.assertTrue(api.normalize_entry({"_from_archive": True})["archived"])

    def test_preserves_tags(self):
        self.assertEqual(api.normalize_entry({"tags": ["ticket:K-2026-9"]})["tags"], ["ticket:K-2026-9"])


if __name__ == "__main__":
    unittest.main()
