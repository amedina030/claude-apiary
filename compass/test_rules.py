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


def build(**kwargs):
    kwargs.setdefault("heuristic_turns", [])
    return rules.build(**kwargs)


class RenderAndBuildTests(unittest.TestCase):
    def test_seed_round_trips_with_zero_events(self):
        seed = store.load_seed_rules()
        first = build(seed=seed, manual_rows=[], events=[], now=NOW)
        second = build(seed=seed, manual_rows=[], events=[], now=NOW + timedelta(days=40))
        self.assertEqual(first["text"], second["text"])
        for row in seed["rules"]:
            self.assertIn(f"- **{row['id']}** (", first["text"])
        for item in seed["self_check"]["items"]:
            self.assertIn(item, first["text"])
        self.assertNotIn("## Flags", first["text"])
        self.assertNotIn("## Proposed rules", first["text"])
        self.assertIn("seed 0.50", first["text"])
        self.assertNotIn("; mined ", first["text"])
        # Seed rows with no events carry no evidence line — that is the budget.
        self.assertNotIn("- evidence:", first["text"])
        self.assertNotIn("heuristics", first["text"])
        self.assertTrue(first["text"].endswith("\n"))

    def test_shipped_seed_table_fits_the_startup_budget(self):
        # Every row goes out at every session start (D-2026-62: about 1,000
        # tokens). Chars/4 is a rough token estimate; the bound is the alarm.
        text = build(seed=store.load_seed_rules(), manual_rows=[], events=[], now=NOW)["text"]
        self.assertLess(len(text), 5200, msg=f"seed rules.md is {len(text)} chars")

    def test_events_flip_source_to_mined_and_show_evidence(self):
        result = build(
            seed=SEED, manual_rows=[], events=[event("J4", "confirm", quote="ship it")], now=NOW
        )
        text = result["text"]
        self.assertIn("- **J4** (specific, J1; mined 0.75) Lean on prior art.", text)
        self.assertIn('  - evidence: 1 confirmed, 0 contradicted, last 2026-09-06; "ship it"', text)
        self.assertIn("- **J1** (principle; mined 0.75) Prefer thorough.", text)  # rolled up
        self.assertIn("from 1 event(s) across 1 session(s)", text)

    def test_flags_and_proposals_render_as_sections(self):
        events = [event("J4", "contradict", 2), event("J4", "contradict", 1)] + [
            event(None, "confirm", action="name the ci job", sid=s) for s in "abc"
        ]
        result = build(seed=SEED, manual_rows=[], events=events, now=NOW)
        self.assertEqual(result["flagged"], ["J4"])
        self.assertIn("## Flags", result["text"])
        self.assertIn("(specific, J1; mined 0.17; FLAGGED) Lean on prior art.", result["text"])
        self.assertIn("## Proposed rules", result["text"])
        self.assertIn("- [judgment] name the ci job (3 events, 3 sessions)", result["text"])

    def test_expiry_shows_on_the_header(self):
        manual = [
            {
                "id": "T2",
                "section": "output",
                "kind": "specific",
                "parent": "O1",
                "rule": "Usage is tight this week.",
                "why": "w",
                "expiry": "2026-09-30",
            }
        ]
        text = build(seed=SEED, manual_rows=manual, events=[], now=NOW)["text"]
        self.assertIn("- **T2** (specific, O1; manual 0.50; expires 2026-09-30) Usage", text)

    def test_heuristics_summarise_under_output_only(self):
        turns = [
            {
                "session_id": "a",
                "outcome_first": True,
                "one_recommendation": True,
                "length_band": False,
            },
            {
                "session_id": "a",
                "outcome_first": False,
                "one_recommendation": True,
                "length_band": True,
            },
            {
                "session_id": "b",
                "outcome_first": True,
                "one_recommendation": False,
                "length_band": True,
            },
            {
                "session_id": "b",
                "outcome_first": True,
                "one_recommendation": True,
                "length_band": True,
            },
        ]
        result = build(seed=SEED, manual_rows=[], events=[], heuristic_turns=turns, now=NOW)
        text = result["text"]
        line = next(ln for ln in text.splitlines() if ln.startswith("- heuristics"))
        self.assertIn("4 turn(s) in 2 session(s)", line)
        self.assertIn("outcome in the first sentence 75%", line)
        self.assertIn("at most one recommendation 75%", line)
        self.assertIn("length band 150-3000 chars 75%", line)
        judgment = text.split("## Output")[0]
        self.assertNotIn("heuristics", judgment)
        self.assertEqual(result["heuristics"]["turns"], 4)
        # A heuristic row never touches a rule's confidence.
        self.assertIn("(principle; seed 0.50) Lead with the outcome.", text)

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


class DeliveryTests(unittest.TestCase):
    """parse_rules_md / pin_text / rule_line read the rendered table back."""

    def rendered(self, events=(), manual=()):
        return build(seed=SEED, manual_rows=list(manual), events=list(events), now=NOW)["text"]

    def test_parse_recovers_rows_why_flags_and_self_check(self):
        events = [event("J4", "contradict", 2), event("J4", "contradict", 1)]
        parsed = rules.parse_rules_md(self.rendered(events))
        by_id = {r["id"]: r for r in parsed["rows"]}
        self.assertEqual(sorted(by_id), ["J1", "J4", "O1"])
        self.assertEqual(by_id["J1"]["kind"], "principle")
        self.assertIsNone(by_id["J1"]["parent"])
        self.assertEqual(by_id["J1"]["rule"], "Prefer thorough.")
        self.assertEqual(by_id["J1"]["why"], "you do")
        self.assertEqual(by_id["J4"]["kind"], "specific")
        self.assertEqual(by_id["J4"]["parent"], "J1")
        self.assertTrue(by_id["J4"]["flagged"])
        self.assertFalse(by_id["J1"]["flagged"])
        self.assertEqual(parsed["self_check"], ["Outcome first?", "One recommendation?"])

    def test_parse_round_trips_the_shipped_seed(self):
        seed = store.load_seed_rules()
        parsed = rules.parse_rules_md(build(seed=seed, manual_rows=[], events=[], now=NOW)["text"])
        self.assertEqual([r["id"] for r in parsed["rows"]], [r["id"] for r in seed["rules"]])
        for got, want in zip(parsed["rows"], seed["rules"]):
            self.assertEqual(got["rule"], want["rule"])
            self.assertEqual(got["why"], want["why"])
            self.assertEqual(got["kind"], want["kind"])
            self.assertEqual(got["parent"], want["parent"])
        self.assertEqual(parsed["self_check"], seed["self_check"]["items"])

    def test_parse_tolerates_junk_and_empty_text(self):
        self.assertEqual(rules.parse_rules_md(""), {"rows": [], "self_check": []})
        parsed = rules.parse_rules_md("- **J1**\n- not a row\n## Self-check\n\nno numbers\n")
        self.assertEqual(parsed, {"rows": [], "self_check": []})

    def test_pin_is_principles_plus_self_check(self):
        pin = rules.pin_text(rules.parse_rules_md(self.rendered()))
        lines = pin.splitlines()
        self.assertTrue(lines[0].startswith("compass rules pin"))
        self.assertIn("J1 Prefer thorough.", lines)
        self.assertIn("O1 Lead with the outcome.", lines)
        self.assertNotIn("J4", pin)  # specific rows stay in the startup block
        self.assertEqual(
            lines[-1], "Self-check before finalizing: Outcome first? One recommendation?"
        )

    def test_shipped_pin_is_small(self):
        seed = store.load_seed_rules()
        pin = rules.pin_text(
            rules.parse_rules_md(build(seed=seed, manual_rows=[], events=[], now=NOW)["text"])
        )
        self.assertLess(len(pin), 1400, msg=f"pin is {len(pin)} chars")
        self.assertEqual(pin.count("\n"), 1 + 8)  # header, 8 principles, self-check

    def test_rule_line_uses_the_rendered_text(self):
        manual = [
            {
                "id": "J4",
                "section": "judgment",
                "kind": "specific",
                "parent": "J1",
                "rule": "Cite the prior art (edited).",
                "why": "w2",
            }
        ]
        parsed = rules.parse_rules_md(self.rendered(manual=manual))
        self.assertEqual(rules.rule_line(parsed, "J4"), "J4 Cite the prior art (edited). (why: w2)")
        self.assertIsNone(rules.rule_line(parsed, "J5"))


if __name__ == "__main__":
    unittest.main()
