"""Tests for compass.health — the facts behind ``apiary doctor compass``."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import health, store  # noqa: E402


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name).resolve() / "state"
        self.compass = self.state / "compass"
        self.compass.mkdir(parents=True)

    # --- helpers ---------------------------------------------------------

    def add_turns(self, name: str, pairs: int) -> None:
        folder = self.compass / "turns"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.jsonl").write_text(
            "".join(
                json.dumps({"prompt_id": f"p{i}", "user": "u", "assistant": "a"}) + "\n"
                for i in range(pairs)
            ),
            encoding="utf-8",
        )
        (folder / f"{name}.cursor.json").write_text('{"offset": 1}', encoding="utf-8")

    def add_events(self, name: str, events: int, *, skipped: str | None = None) -> None:
        folder = self.compass / "events"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.json").write_text(
            json.dumps(
                {
                    "session_id": name,
                    "pairs": 6,
                    "events": [{"rule": "J1", "polarity": "confirm"}] * events,
                    "skipped": skipped,
                }
            ),
            encoding="utf-8",
        )

    def add_heuristics(self, name: str, turns: int) -> None:
        folder = self.compass / "events"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}{store.HEURISTICS_SUFFIX}").write_text(
            "".join(json.dumps({"outcome_first": True}) + "\n" for _ in range(turns)),
            encoding="utf-8",
        )

    def write_rules(
        self, *, rows: int = 3, flagged: int = 1, proposed: int = 2, age_days: float = 0.0
    ) -> Path:
        lines = ["# Rules for Claude", ""]
        for i in range(rows):
            label = "principle; seed 0.50" + ("; FLAGGED" if i < flagged else "")
            lines.append(f"- **J{i}** ({label}) Rule {i}.")
        if proposed:
            lines += ["", "## Proposed rules", ""]
            lines += [f"- [judgment] action {i} (3 events, 2 sessions)" for i in range(proposed)]
        lines += ["", "## Self-check", "", "1. Outcome first?"]
        path = self.compass / "rules.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    # --- rule-table pipeline --------------------------------------------

    def test_counts_turns_events_heuristics_and_pending(self):
        self.add_turns("aaaa0001", 6)
        self.add_turns("aaaa0002", 3)
        self.add_turns("aaaa0003", 9)
        self.add_events("aaaa0001", 2)
        self.add_events("aaaa0002", 0, skipped="too_few_pairs")
        self.add_heuristics("aaaa0001", 5)
        self.add_heuristics("aaaa0003", 2)
        facts = health.collect(self.state)
        self.assertEqual(facts["turn_sessions"], 3)
        self.assertEqual(facts["pairs"], 18)
        self.assertEqual(facts["classified_sessions"], 1)
        self.assertEqual(facts["skipped_sessions"], 1)
        self.assertEqual(facts["pending_sessions"], 1)
        self.assertEqual(facts["events"], 2)
        self.assertEqual(facts["heuristic_turns"], 7)
        note = " ".join(health.format_notes(facts))
        self.assertIn("turns: 3 session(s), 18 pair(s)", note)
        self.assertIn("1 classified, 1 skipped, 1 pending -> 2 event(s); 7 heuristic turn(s)", note)

    def test_rules_md_facts(self):
        self.write_rules(rows=4, flagged=1, proposed=2, age_days=2)
        facts = health.collect(self.state)
        self.assertEqual(facts["rules_rows"], 4)
        self.assertEqual(facts["rules_flagged"], 1)
        self.assertEqual(facts["rules_proposed"], 2)
        self.assertAlmostEqual(facts["rules_age_days"], 2, delta=0.1)
        self.assertIn(
            "rules.md: 4 rows, 1 flagged, 2 proposed", " ".join(health.format_notes(facts))
        )

    def test_missing_rules_md(self):
        facts = health.collect(self.state)
        self.assertIsNone(facts["rules_rows"])
        self.assertIn("rules.md: not written yet", " ".join(health.format_notes(facts)))

    def test_go_no_go_note_before_and_after_the_threshold(self):
        note = " ".join(health.format_notes(health.collect(self.state)))
        self.assertIn(f"decide after {health.GO_NO_GO_SESSIONS} captured sessions (0 so far)", note)
        for i in range(health.GO_NO_GO_SESSIONS):
            self.add_turns(f"bbbb{i:04d}", 6)
        self.add_events("bbbb0000", 4)
        note = " ".join(health.format_notes(health.collect(self.state)))
        self.assertIn("NO-GO", note)
        self.add_events("bbbb0001", health.GO_NO_GO_MIN_EVENTS)
        note = " ".join(health.format_notes(health.collect(self.state)))
        self.assertIn("-> GO", note)

    def test_unreadable_events_file_is_skipped(self):
        (self.compass / "events").mkdir()
        (self.compass / "events" / "bad.json").write_text("{", encoding="utf-8")
        facts = health.collect(self.state)
        self.assertEqual(facts["classified_sessions"], 0)
        self.assertEqual(facts["events"], 0)

    def test_missing_state_reports_not_exists(self):
        facts = health.collect(Path(self._tmp.name).resolve() / "nowhere")
        self.assertFalse(facts["exists"])
        self.assertIn("no compass state", health.format_notes(facts)[0])

    def test_retired_profile_is_not_reported(self):
        (self.compass / "personality.md").write_text("# old profile\n", encoding="utf-8")
        facts = health.collect(self.state)
        self.assertNotIn("profile_chars", facts)
        self.assertNotIn("arm_counts", facts)
        note = " ".join(health.format_notes(facts))
        self.assertNotIn("personality", note)
        self.assertNotIn("A/B", note)

    def test_format_notes_returns_only_strings(self):
        self.add_turns("aaaa0001", 6)
        self.write_rules()
        notes = health.format_notes(health.collect(self.state))
        self.assertTrue(notes)
        self.assertTrue(all(isinstance(n, str) for n in notes))


if __name__ == "__main__":
    unittest.main()
