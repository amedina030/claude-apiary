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

from compass import ab, health  # noqa: E402


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name).resolve() / "state"
        self.compass = self.state / "compass"
        self.compass.mkdir(parents=True)
        (self.state / "sessions").mkdir(parents=True)

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

    def write_rules(
        self, *, rows: int = 3, flagged: int = 1, proposed: int = 2, age_days: float = 0.0
    ) -> Path:
        lines = ["# Rules for Claude", ""]
        for i in range(rows):
            lines.append(f"- **J{i}** (principle) Rule {i}.")
            evidence = "  - evidence: confirmed 0, contradicted 0, last seen never; confidence 0.50; source seed"
            if i < flagged:
                evidence += "; FLAGGED"
            lines.append(evidence)
        if proposed:
            lines += ["", "## Proposed rules", ""]
            lines += [f"- [judgment] action {i} (3 events, 2 sessions)" for i in range(proposed)]
        lines += ["", "## Self-check", "", "1. Outcome first?"]
        path = self.compass / "rules.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def write_profile(self, text: str, age_days: float = 0.0) -> Path:
        path = self.compass / "personality.md"
        path.write_text(text, encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def write_identity(self, name: str, arm=None) -> None:
        payload = {"role": "user", "mission": "general"}
        if arm is not None:
            payload["compass_arm"] = arm
        (self.state / "sessions" / f"identity-{name}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    # --- rule-table pipeline --------------------------------------------

    def test_counts_turns_events_and_pending(self):
        self.add_turns("aaaa0001", 6)
        self.add_turns("aaaa0002", 3)
        self.add_turns("aaaa0003", 9)
        self.add_events("aaaa0001", 2)
        self.add_events("aaaa0002", 0, skipped="too_few_pairs")
        facts = health.collect(self.state)
        self.assertEqual(facts["turn_sessions"], 3)
        self.assertEqual(facts["pairs"], 18)
        self.assertEqual(facts["classified_sessions"], 1)
        self.assertEqual(facts["skipped_sessions"], 1)
        self.assertEqual(facts["pending_sessions"], 1)
        self.assertEqual(facts["events"], 2)
        note = " ".join(health.format_notes(facts))
        self.assertIn("turns: 3 session(s), 18 pair(s)", note)
        self.assertIn("1 classified, 1 skipped, 1 pending -> 2 event(s)", note)

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

    # --- legacy profile / A/B / evaluate (live until T-2026-320) ---------

    def test_missing_state_reports_not_exists(self):
        facts = health.collect(Path(self._tmp.name).resolve() / "nowhere")
        self.assertFalse(facts["exists"])
        self.assertIn("no compass state", health.format_notes(facts)[0])

    def test_profile_length_and_age(self):
        self.write_profile("# Profile\n", age_days=3)
        facts = health.collect(self.state)
        self.assertEqual(facts["profile_chars"], len("# Profile\n"))
        self.assertAlmostEqual(facts["profile_age_days"], 3, delta=0.1)
        self.assertFalse(facts["stale"])

    def test_stale_profile_is_flagged(self):
        self.write_profile("# Profile\n", age_days=health.STALE_SYNTHESIS_DAYS + 1)
        facts = health.collect(self.state)
        self.assertTrue(facts["stale"])
        self.assertIn("STALE", " ".join(health.format_notes(facts)))

    def test_missing_profile(self):
        facts = health.collect(self.state)
        self.assertIsNone(facts["profile_chars"])
        self.assertIn("not written yet", " ".join(health.format_notes(facts)))

    def test_arm_counts(self):
        self.write_identity("aaaa0001", ab.ARM_ON)
        self.write_identity("aaaa0002", ab.ARM_OFF)
        self.write_identity("aaaa0003", ab.ARM_OFF)
        self.write_identity("aaaa0004")  # pre-A/B session
        counts = health.collect(self.state)["arm_counts"]
        self.assertEqual(counts["on"], 1)
        self.assertEqual(counts["off"], 2)
        self.assertEqual(counts["unrecorded"], 1)

    def test_unreadable_identity_is_skipped(self):
        (self.state / "sessions" / "identity-bad.json").write_text("{", encoding="utf-8")
        self.assertEqual(health.collect(self.state)["arm_counts"]["unrecorded"], 0)

    def test_cached_evaluate_headline_is_surfaced(self):
        folder = self.compass / "evaluate"
        folder.mkdir(parents=True)
        (folder / "last.json").write_text(
            json.dumps(
                {
                    "headline": 0.83,
                    "lift_over_majority": 0.04,
                    "mode": "model",
                    "folds": 12,
                    "computed_at": "2026-08-26T10:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        facts = health.collect(self.state)
        self.assertEqual(facts["last_evaluate"]["headline"], 0.83)
        note = " ".join(health.format_notes(facts))
        self.assertIn("83.0%", note)
        self.assertIn("+4.0 pts", note)

    def test_missing_evaluate_cache(self):
        self.assertIn("never run", " ".join(health.format_notes(health.collect(self.state))))

    def test_notes_mention_the_ab_state(self):
        note = " ".join(health.format_notes(health.collect(self.state)))
        self.assertIn("A/B:", note)

    def test_format_notes_returns_only_strings(self):
        self.add_turns("aaaa0001", 6)
        self.write_rules()
        self.write_profile("x")
        notes = health.format_notes(health.collect(self.state))
        self.assertTrue(notes)
        self.assertTrue(all(isinstance(n, str) for n in notes))


if __name__ == "__main__":
    unittest.main()
