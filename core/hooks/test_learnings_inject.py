#!/usr/bin/env python3
"""Unit tests for the pure score_learnings function."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.hooks.learnings_inject_hook import (
    _area_is_specific,
    _tokenize_command,
    load_seen_ids,
    record_seen_ids,
    score_learnings,
    seen_ids_path,
)


def _entry(display_id: str, *, tags=None, areas=None, timestamp=""):
    """Minimal fixture that mirrors an index.jsonl entry."""
    year, seq = 2026, int(display_id.rsplit("-", 1)[-1])
    return {
        "display_id": display_id,
        "type": "learning",
        "year": year,
        "seq": seq,
        "summary": f"summary for {display_id}",
        "tags": list(tags or []),
        "areas": list(areas or []),
        "timestamp": timestamp,
    }


class TestTokenizeCommand(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_tokenize_command(""), [])
        self.assertEqual(_tokenize_command(None or ""), [])

    def test_strips_flags_and_paths(self):
        toks = _tokenize_command("python -m build --verbose /tmp/out.log")
        self.assertIn("python", toks)
        self.assertIn("build", toks)
        self.assertNotIn("--verbose", toks)
        self.assertNotIn("/tmp/out.log", toks)

    def test_lowercases(self):
        self.assertEqual(_tokenize_command("PyInstaller --build"), ["pyinstaller", "build"])


class TestAreaIsSpecific(unittest.TestCase):
    def test_empty_is_not_specific(self):
        self.assertFalse(_area_is_specific(""))

    def test_bare_wildcards_not_specific(self):
        self.assertFalse(_area_is_specific("*"))
        self.assertFalse(_area_is_specific("**"))

    def test_path_with_slash_is_specific(self):
        self.assertTrue(_area_is_specific("gui/**"))
        self.assertTrue(_area_is_specific("scribe/notes.py"))

    def test_no_slash_no_leading_star_is_specific(self):
        self.assertTrue(_area_is_specific("notes.py"))


class TestScoreLearnings(unittest.TestCase):
    def test_empty_entries_returns_empty(self):
        self.assertEqual(score_learnings([], target_path="gui/x.js"), [])

    def test_no_match_returns_empty(self):
        entries = [_entry("L-2026-1", areas=["scribe/*"])]
        self.assertEqual(score_learnings(entries, target_path="gui/x.js"), [])

    def test_area_match_fires(self):
        entries = [
            _entry("L-2026-1", areas=["gui/**"]),
            _entry("L-2026-2", areas=["scribe/*"]),
        ]
        top = score_learnings(entries, target_path="gui/web/app.js")
        self.assertEqual([e["display_id"] for e in top], ["L-2026-1"])
        self.assertEqual(top[0]["_matched_area"], "gui/**")

    def test_specific_area_outranks_broad(self):
        entries = [
            _entry("L-2026-1", areas=["**"]),
            _entry("L-2026-2", areas=["gui/web/*"]),
        ]
        top = score_learnings(entries, target_path="gui/web/app.js")
        self.assertEqual(top[0]["display_id"], "L-2026-2")

    def test_tag_match_on_bash_command(self):
        entries = [
            _entry("L-2026-1", tags=["pyinstaller"]),
            _entry("L-2026-2", tags=["windows"]),
        ]
        top = score_learnings(entries, command="python -m pyinstaller gui/app.py")
        self.assertEqual(top[0]["display_id"], "L-2026-1")
        self.assertEqual(top[0]["_matched_tag"], "pyinstaller")

    def test_top_n_caps_results(self):
        entries = [_entry(f"L-2026-{i}", areas=["gui/**"]) for i in range(1, 10)]
        top = score_learnings(entries, target_path="gui/x.js", top_n=3)
        self.assertEqual(len(top), 3)

    def test_zero_top_n_returns_empty(self):
        entries = [_entry("L-2026-1", areas=["gui/**"])]
        self.assertEqual(score_learnings(entries, target_path="gui/x.js", top_n=0), [])

    def test_ties_broken_by_timestamp_newest_first(self):
        entries = [
            _entry("L-2026-3", areas=["gui/**"], timestamp="2026-01-03T00:00:00Z"),
            _entry("L-2026-7", areas=["gui/**"], timestamp="2026-01-07T00:00:00Z"),
            _entry("L-2026-5", areas=["gui/**"], timestamp="2026-01-05T00:00:00Z"),
        ]
        top = score_learnings(entries, target_path="gui/x.js")
        # All same score (all inside the recency window) → newest first.
        self.assertEqual([e["display_id"] for e in top], ["L-2026-7", "L-2026-5", "L-2026-3"])

    def test_equal_timestamps_fall_back_to_id_descending(self):
        entries = [
            _entry("L-2026-3", areas=["gui/**"]),
            _entry("L-2026-7", areas=["gui/**"]),
            _entry("L-2026-5", areas=["gui/**"]),
        ]
        top = score_learnings(entries, target_path="gui/x.js")
        # No timestamps at all → higher seq (newer) wins, deterministically.
        self.assertEqual([e["display_id"] for e in top], ["L-2026-7", "L-2026-5", "L-2026-3"])

    def test_all_wildcard_segment_globs_are_broad(self):
        # `**/*.py` matches most of any repo — it must not outrank a
        # subsystem glob like `runner/**` on every .py edit.
        for glob in ("**/*.py", "*/*", "*.py", "**/*"):
            with self.subTest(glob=glob):
                self.assertFalse(_area_is_specific(glob))
        for glob in ("gui/**", "scribe/notes.py", "docs/*/ref.md"):
            with self.subTest(glob=glob):
                self.assertTrue(_area_is_specific(glob))

    def test_broad_catchall_loses_to_subsystem_glob(self):
        entries = [
            _entry("L-2026-1", areas=["**/*.py"]),
            _entry("L-2026-2", areas=["runner/**"]),
        ]
        top = score_learnings(entries, target_path="runner/auto_plan.py", top_n=1)
        self.assertEqual(top[0]["display_id"], "L-2026-2")

    def test_area_and_tag_accumulate(self):
        entry = _entry("L-2026-1", areas=["gui/**"], tags=["pyinstaller"])
        top = score_learnings(
            [entry],
            target_path="gui/web/app.js",
            command="pyinstaller --onefile",
        )
        self.assertEqual(len(top), 1)
        # Area specific (1.0) + tag match (0.2) = 1.2 minimum before recency.
        self.assertGreaterEqual(top[0]["_match_score"], 1.2)

    def test_recency_bonus_breaks_ties_between_same_score(self):
        # Two entries compete on identical area-specificity. The tied
        # newer one must sit in the recency window (top 10 by timestamp)
        # AND the older one must fall outside it — otherwise the recency
        # bump cancels and the ID-ascending fallback wins. Pad with 10
        # filler entries newer than the old one so the window excludes it.
        entries = [
            _entry("L-2026-1", areas=["gui/**"], timestamp="2026-01-01T00:00:00Z"),
            _entry("L-2026-99", areas=["gui/**"], timestamp="2026-04-20T00:00:00Z"),
        ]
        for i in range(10, 20):
            entries.append(
                _entry(f"L-2026-{i}", areas=["scribe/*"], timestamp=f"2026-03-{i:02d}T00:00:00Z")
            )
        top = score_learnings(entries, target_path="gui/x.js")
        # Only L-2026-1 and L-2026-99 hit gui/**. L-2026-99 is inside the
        # recency window; L-2026-1 is not. Newer wins.
        self.assertEqual(top[0]["display_id"], "L-2026-99")

    def test_no_target_no_command_returns_empty(self):
        entries = [_entry("L-2026-1", areas=["**"], tags=["python"])]
        self.assertEqual(score_learnings(entries), [])

    def test_missing_tags_areas_fields_safe(self):
        """Legacy entries with no tags/areas fields must not crash the scorer."""
        entries = [{"display_id": "L-2026-1", "year": 2026, "seq": 1, "summary": "legacy"}]
        self.assertEqual(score_learnings(entries, target_path="x"), [])


class SessionDedupTests(unittest.TestCase):
    """The per-session seen-file that stops repeat injections."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_follows_session_tmp_convention(self):
        p = seen_ids_path(self.repo, "abc12345")
        self.assertEqual(
            p,
            self.repo / ".claude" / "apiary" / "session-tmp" / "abc12345_learnings_injected",
        )

    def test_load_missing_file_is_empty(self):
        self.assertEqual(load_seen_ids(seen_ids_path(self.repo, "abc12345")), set())

    def test_record_then_load_round_trips(self):
        p = seen_ids_path(self.repo, "abc12345")
        record_seen_ids(p, ["L-2026-1", "L-2026-2"])
        self.assertEqual(load_seen_ids(p), {"L-2026-1", "L-2026-2"})

    def test_record_appends_across_calls(self):
        p = seen_ids_path(self.repo, "abc12345")
        record_seen_ids(p, ["L-2026-1"])
        record_seen_ids(p, ["L-2026-2"])
        self.assertEqual(load_seen_ids(p), {"L-2026-1", "L-2026-2"})

    def test_record_failure_is_silent(self):
        # A file where the parent dir should be → mkdir fails; must not raise.
        blocker = self.repo / "blocked"
        blocker.write_text("x", encoding="utf-8")
        record_seen_ids(blocker / "sub" / "seen", ["L-2026-1"])


if __name__ == "__main__":
    unittest.main()
