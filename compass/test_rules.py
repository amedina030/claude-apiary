"""Tests for compass.rules — decay math, aggregation, flags, proposals, rendering."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import rules, store  # noqa: E402

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

SEED = {
    "sections": [
        {"id": "judgment", "title": "Judgment", "subtitle": "what to decide"},
        {"id": "output", "title": "Output", "subtitle": "how to write"},
    ],
    "rules": [
        {
            "id": "J1",
            "section": "judgment",
            "kind": "principle",
            "parent": None,
            "rule": "Prefer thorough.",
            "why": "you do",
            "source": "seed",
        },
        {
            "id": "J4",
            "section": "judgment",
            "kind": "specific",
            "parent": "J1",
            "rule": "Lean on prior art.",
            "why": "memory",
            "source": "seed",
        },
        {
            "id": "O1",
            "section": "output",
            "kind": "principle",
            "parent": None,
            "rule": "Lead with the outcome.",
            "why": "one round",
            "source": "seed",
        },
    ],
    "self_check": {"title": "Self-check", "items": ["Outcome first?", "One recommendation?"]},
}


def event(rule, polarity, days_ago=0.0, *, section="judgment", action="did x", quote="q", sid="s1"):
    return {
        "rule": rule,
        "polarity": polarity,
        "type": "correction" if polarity == "contradict" else "acceptance",
        "section": section,
        "action": action,
        "quote": quote,
        "ts": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": sid,
    }


class DecayAndConfidenceTests(unittest.TestCase):
    def test_half_life(self):
        self.assertAlmostEqual(rules.decay_weight(NOW, NOW), 1.0)
        self.assertAlmostEqual(rules.decay_weight(NOW - timedelta(days=60), NOW), 0.5)
        self.assertAlmostEqual(rules.decay_weight(NOW - timedelta(days=120), NOW), 0.25)

    def test_future_or_undated_events_weigh_one(self):
        self.assertEqual(rules.decay_weight(NOW + timedelta(days=3), NOW), 1.0)
        self.assertEqual(rules.decay_weight(None, NOW), 1.0)
        self.assertEqual(rules.decay_weight("not a date", NOW), 1.0)

    def test_confidence_from_counts(self):
        self.assertEqual(rules.confidence(0, 0), 0.5)
        self.assertEqual(rules.confidence(3, 0), 0.88)
        self.assertEqual(rules.confidence(0, 3), 0.12)
        self.assertEqual(rules.confidence(2, 2), 0.5)


class AggregateTests(unittest.TestCase):
    def rows(self):
        return rules.merge_rows(SEED["rules"], [], NOW)

    def test_specific_events_roll_up_to_the_parent_principle(self):
        stats = rules.aggregate(self.rows(), [event("J4", "confirm")], NOW)
        self.assertEqual(stats["J4"]["n_confirm"], 1)
        self.assertEqual(stats["J1"]["n_confirm"], 1)
        self.assertEqual(stats["O1"]["n_confirm"], 0)
        self.assertEqual(stats["J4"]["quote"], "q")
        self.assertEqual(stats["J4"]["n_sessions"], 1)

    def test_weights_decay_and_confidence_follows(self):
        stats = rules.aggregate(
            self.rows(), [event("O1", "confirm", days_ago=60), event("O1", "contradict")], NOW
        )
        self.assertAlmostEqual(stats["O1"]["confirmed"], 0.5)
        self.assertAlmostEqual(stats["O1"]["contradicted"], 1.0)
        self.assertEqual(stats["O1"]["confidence"], rules.confidence(0.5, 1.0))

    def test_unknown_rule_or_polarity_is_ignored(self):
        stats = rules.aggregate(self.rows(), [event("ZZ9", "confirm"), event("J1", "maybe")], NOW)
        self.assertEqual(stats["J1"]["n_confirm"], 0)

    def test_latest_quote_wins(self):
        stats = rules.aggregate(
            self.rows(),
            [event("J1", "confirm", days_ago=5, quote="old"), event("J1", "confirm", quote="new")],
            NOW,
        )
        self.assertEqual(stats["J1"]["quote"], "new")
        self.assertEqual(stats["J1"]["last_seen"].date(), NOW.date())


class FlagAndProposalTests(unittest.TestCase):
    def rows(self):
        return rules.merge_rows(SEED["rules"], [], NOW)

    def test_specific_row_contradicted_twice_in_a_row_is_flagged(self):
        events = [
            event("J4", "confirm", 3),
            event("J4", "contradict", 2),
            event("J4", "contradict", 1),
        ]
        stats = rules.aggregate(self.rows(), events, NOW)
        self.assertEqual(rules.flagged_rules(self.rows(), stats), ["J4"])

    def test_principles_are_never_flagged_and_order_matters(self):
        events = [event("J1", "contradict", 2), event("J1", "contradict", 1)]
        stats = rules.aggregate(self.rows(), events, NOW)
        self.assertEqual(rules.flagged_rules(self.rows(), stats), [])
        events = [event("J4", "contradict", 2), event("J4", "confirm", 1)]
        stats = rules.aggregate(self.rows(), events, NOW)
        self.assertEqual(rules.flagged_rules(self.rows(), stats), [])

    def test_three_unattached_events_with_one_action_propose_a_row(self):
        events = [
            event(None, "confirm", action="Name the CI job!", sid="a"),
            event(None, "confirm", action="name the ci job", sid="b"),
            event(None, "contradict", action="  Name, the CI job ", sid="a"),
            event(None, "confirm", action="something else"),
            event(None, "confirm", action="something else"),
        ]
        proposed = rules.proposals(events, self.rows())
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["action"], "name the ci job")
        self.assertEqual(proposed[0]["events"], 3)
        self.assertEqual(proposed[0]["sessions"], 2)

    def test_attached_events_never_propose(self):
        events = [event("J1", "confirm", action="same")] * 5
        self.assertEqual(rules.proposals(events, self.rows()), [])


class MergeRowsTests(unittest.TestCase):
    def test_manual_row_overrides_seed_in_place_and_new_rows_append(self):
        manual = [
            {
                "id": "J1",
                "section": "judgment",
                "kind": "principle",
                "parent": None,
                "rule": "Prefer thorough (edited).",
                "why": "w",
            },
            {
                "id": "J7",
                "section": "judgment",
                "kind": "specific",
                "parent": "J1",
                "rule": "New one.",
                "why": "w",
            },
        ]
        rows = rules.merge_rows(SEED["rules"], manual, NOW)
        self.assertEqual([r["id"] for r in rows], ["J1", "J4", "O1", "J7"])
        self.assertEqual(rows[0]["rule"], "Prefer thorough (edited).")
        self.assertEqual(rows[0]["source"], "manual")
        self.assertEqual(rows[1]["source"], "seed")

    def test_expired_rows_drop_and_future_expiry_stays(self):
        manual = [
            {
                "id": "T1",
                "section": "output",
                "kind": "specific",
                "parent": "O1",
                "rule": "Usage is tight this week.",
                "why": "w",
                "expiry": "2026-09-05",
            },
            {
                "id": "T2",
                "section": "output",
                "kind": "specific",
                "parent": "O1",
                "rule": "Still on.",
                "why": "w",
                "expiry": "2026-09-07",
            },
        ]
        rows = rules.merge_rows(SEED["rules"], manual, NOW)
        self.assertEqual([r["id"] for r in rows][-1], "T2")
        self.assertNotIn("T1", [r["id"] for r in rows])

    def test_load_manual_rows_tolerates_missing_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules_manual.json"
            self.assertEqual(rules.load_manual_rows(path), [])
            path.write_text("{", encoding="utf-8")
            self.assertEqual(rules.load_manual_rows(path), [])
            path.write_text(
                json.dumps({"rules": [{"id": "J9", "rule": "x"}, {"id": "bad id"}]}),
                encoding="utf-8",
            )
            self.assertEqual([r["id"] for r in rules.load_manual_rows(path)], ["J9"])


class RenderAndBuildTests(unittest.TestCase):
    def test_seed_round_trips_with_zero_events(self):
        seed = store.load_seed_rules()
        first = rules.build(seed=seed, manual_rows=[], events=[], now=NOW)
        second = rules.build(seed=seed, manual_rows=[], events=[], now=NOW + timedelta(days=40))
        self.assertEqual(first["text"], second["text"])
        for row in seed["rules"]:
            self.assertIn(f"- **{row['id']}** (", first["text"])
        for item in seed["self_check"]["items"]:
            self.assertIn(item, first["text"])
        self.assertNotIn("## Flags", first["text"])
        self.assertNotIn("## Proposed rules", first["text"])
        self.assertIn("source seed", first["text"])
        self.assertNotIn("source mined", first["text"])
        self.assertTrue(first["text"].endswith("\n"))

    def test_events_flip_source_to_mined_and_show_evidence(self):
        result = rules.build(
            seed=SEED, manual_rows=[], events=[event("J4", "confirm", quote="ship it")], now=NOW
        )
        text = result["text"]
        self.assertIn(
            "confirmed 1, contradicted 0, last seen 2026-09-06; confidence 0.75; source mined", text
        )
        self.assertIn('quote: "ship it"', text)
        self.assertIn("from 1 event(s) across 1 session(s)", text)

    def test_flags_and_proposals_render_as_sections(self):
        events = [event("J4", "contradict", 2), event("J4", "contradict", 1)] + [
            event(None, "confirm", action="name the ci job", sid=s) for s in "abc"
        ]
        result = rules.build(seed=SEED, manual_rows=[], events=events, now=NOW)
        self.assertEqual(result["flagged"], ["J4"])
        self.assertIn("## Flags", result["text"])
        self.assertIn("FLAGGED", result["text"])
        self.assertIn("## Proposed rules", result["text"])
        self.assertIn("- [judgment] name the ci job (3 events, 3 sessions)", result["text"])

    def test_rule_ids_is_the_classifier_vocabulary(self):
        self.assertEqual(rules.rule_ids(seed=SEED, manual_rows=[]), ["J1", "J4", "O1"])

    def test_cli_build_write_then_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve() / "state"
            with mock.patch.dict(os.environ, {store.TARGET_STATE_DIR_ENV: str(state)}):
                self.assertEqual(rules.main(["build", "--check"]), 1)  # nothing on disk yet
                self.assertEqual(rules.main(["build", "--write"]), 0)
                target = store.rules_path()
                self.assertTrue(target.is_file())
                self.assertEqual(rules.main(["build", "--check"]), 0)
                target.write_text(target.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
                self.assertEqual(rules.main(["build", "--check"]), 1)
                self.assertEqual(rules.main(["build", "--now", "garbage"]), 2)


if __name__ == "__main__":
    unittest.main()
