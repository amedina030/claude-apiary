"""Tests for compass.heuristics — the Stop-hook output heuristics."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import heuristics, store  # noqa: E402


class ScoringTests(unittest.TestCase):
    def test_first_sentence_strips_markdown_and_stops_at_the_period(self):
        self.assertEqual(
            heuristics.first_sentence("## **Done.** Tests pass.\n\nMore."),
            "Done.",
        )
        self.assertEqual(
            heuristics.first_sentence("- Merged as PR #5! Next steps..."), "Merged as PR #5!"
        )
        self.assertEqual(heuristics.first_sentence("no terminator at all"), "no terminator at all")
        self.assertEqual(heuristics.first_sentence("   "), "")

    def test_outcome_first_rejects_narration_questions_and_paragraphs(self):
        self.assertTrue(heuristics.outcome_first("Merged. CI is green on master."))
        self.assertTrue(
            heuristics.outcome_first("The bug is in turns.py: the carry is never reset.")
        )
        self.assertFalse(heuristics.outcome_first("I'll start by reading the handoff."))
        self.assertFalse(heuristics.outcome_first("Let me check the registry first."))
        self.assertFalse(heuristics.outcome_first("Now running the suite."))
        self.assertFalse(heuristics.outcome_first("Here's what I found."))
        self.assertFalse(heuristics.outcome_first("Should I merge it?"))
        self.assertFalse(heuristics.outcome_first(" ".join(["word"] * 41) + "."))
        self.assertFalse(heuristics.outcome_first(""))

    def test_one_recommendation_counts_markers_and_menus(self):
        self.assertTrue(heuristics.one_recommendation("Done. I recommend merging tonight."))
        self.assertTrue(heuristics.one_recommendation("No recommendation here, just a report."))
        self.assertFalse(
            heuristics.one_recommendation("I recommend A. Alternatively B. Alternatively C.")
        )
        self.assertFalse(heuristics.one_recommendation("Option A: keep it. Option B: drop it."))
        self.assertFalse(heuristics.one_recommendation("I suggest X, though I'd go with Y."))

    def test_length_band(self):
        low, high = heuristics.LENGTH_BAND
        self.assertFalse(heuristics.length_band("x" * (low - 1)))
        self.assertTrue(heuristics.length_band("x" * low))
        self.assertTrue(heuristics.length_band("x" * high))
        self.assertFalse(heuristics.length_band("x" * (high + 1)))

    def test_score_output_shape(self):
        text = "Fixed. " + "The cursor now resets on prune. " * 8
        score = heuristics.score_output(text)
        self.assertEqual(
            sorted(score), ["chars", "length_band", "one_recommendation", "outcome_first"]
        )
        self.assertEqual(score["chars"], len(text.strip()))
        self.assertTrue(score["outcome_first"])


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name).resolve() / "state"
        patcher = mock.patch.dict(os.environ, {store.TARGET_STATE_DIR_ENV: str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_record_and_load_session(self):
        row = heuristics.record_turn("abcd1234-0000", "Done. Merged.", "2026-09-06T00:00:00Z")
        self.assertEqual(row["source"], "heuristic")
        heuristics.record_turn("abcd1234-0000", "I'll look.", "2026-09-06T00:01:00Z")
        rows = heuristics.load_session("abcd1234")
        self.assertEqual([r["ts"] for r in rows], ["2026-09-06T00:00:00Z", "2026-09-06T00:01:00Z"])
        self.assertEqual([r["outcome_first"] for r in rows], [True, False])
        self.assertEqual(rows[0]["session_id"], "abcd1234")
        self.assertEqual(heuristics.load_session("ffff0000"), [])

    def test_load_classified_only_reads_sessions_with_an_events_file(self):
        heuristics.record_turn("aaaa0001", "Done.", None)
        heuristics.record_turn("aaaa0002", "Done.", None)
        events = store.events_dir()
        (events / "aaaa0001.json").write_text(json.dumps({"events": []}), encoding="utf-8")
        rows = heuristics.load_classified()
        self.assertEqual([r["session_id"] for r in rows], ["aaaa0001"])
        # The heuristics file is never mistaken for an events file.
        self.assertEqual(sorted(p.name for p in events.glob("*.json")), ["aaaa0001.json"])

    def test_summarize(self):
        rows = [
            {
                "session_id": "a",
                "outcome_first": True,
                "one_recommendation": False,
                "length_band": True,
            },
            {
                "session_id": "b",
                "outcome_first": False,
                "one_recommendation": True,
                "length_band": True,
            },
        ]
        self.assertEqual(
            heuristics.summarize(rows),
            {
                "turns": 2,
                "sessions": 2,
                "outcome_first": 1,
                "one_recommendation": 1,
                "length_band": 2,
            },
        )
        self.assertEqual(heuristics.summarize([])["turns"], 0)


if __name__ == "__main__":
    unittest.main()
