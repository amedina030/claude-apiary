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
        self.state = Path(self._tmp.name) / "state"
        self.compass = self.state / "compass"
        self.observations = self.compass / "observations"
        self.observations.mkdir(parents=True)
        (self.state / "sessions").mkdir(parents=True)

    # --- helpers ---------------------------------------------------------

    def add_observation(self, name: str) -> None:
        (self.observations / f"{name}.json").write_text(
            json.dumps({"session_id": name, "captured_at": "2026-01-01T00:00:00Z",
                        "observations": []}), encoding="utf-8")

    def add_archived(self, week: str, name: str) -> None:
        folder = self.observations / "archive" / week
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text("{}", encoding="utf-8")

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
            json.dumps(payload), encoding="utf-8")

    # --- collect ---------------------------------------------------------

    def test_counts_active_and_archived(self):
        self.add_observation("aaaa0001")
        self.add_observation("aaaa0002")
        self.add_archived("2026-01", "old0001")
        facts = health.collect(self.state)
        self.assertEqual(facts["active_observations"], 2)
        self.assertEqual(facts["archived_observations"], 1)

    def test_missing_state_reports_not_exists(self):
        facts = health.collect(Path(self._tmp.name) / "nowhere")
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
        (folder / "last.json").write_text(json.dumps({
            "headline": 0.83, "lift_over_majority": 0.04, "mode": "model",
            "folds": 12, "computed_at": "2026-08-26T10:00:00+00:00"}), encoding="utf-8")
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
        self.add_observation("aaaa0001")
        self.write_profile("x")
        notes = health.format_notes(health.collect(self.state))
        self.assertTrue(notes)
        self.assertTrue(all(isinstance(n, str) for n in notes))


if __name__ == "__main__":
    unittest.main()
