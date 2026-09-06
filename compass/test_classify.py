"""Tests for compass.classify — reply parsing, vocabulary validation, the session flow."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass import classify, rules, store  # noqa: E402

SID = "abcd1234-1111-2222-3333-444444444444"
ROWS = [
    {
        "id": "J1",
        "section": "judgment",
        "kind": "principle",
        "parent": None,
        "rule": "Prefer thorough.",
    },
    {"id": "O3", "section": "output", "kind": "specific", "parent": "O1", "rule": "Ask in prose."},
]


def pairs(n: int) -> list[dict]:
    return [
        {
            "prompt_id": f"p{i:02d}",
            "ts": f"2026-09-06T00:00:{i:02d}Z",
            "assistant": f"A{i}",
            "user": f"U{i}",
        }
        for i in range(n)
    ]


class ExtractJsonTests(unittest.TestCase):
    def test_envelope_result_with_fence(self):
        inner = json.dumps({"events": []})
        stdout = json.dumps({"result": f"```json\n{inner}\n```"})
        self.assertEqual(classify.extract_json(stdout), {"events": []})

    def test_raw_object_and_prose_wrapped_object(self):
        self.assertEqual(classify.extract_json('{"events": [1]}'), {"events": [1]})
        stdout = json.dumps({"result": 'Here you go: {"events": []} hope that helps'})
        self.assertEqual(classify.extract_json(stdout), {"events": []})

    def test_garbage_is_none(self):
        self.assertIsNone(classify.extract_json(""))
        self.assertIsNone(classify.extract_json("no json here"))
        self.assertIsNone(classify.extract_json(json.dumps({"result": "[1, 2]"})))


class ValidateEventsTests(unittest.TestCase):
    def good(self, **over):
        item = {
            "pair": 1,
            "type": "acceptance",
            "section": "judgment",
            "rule": "J1",
            "polarity": "confirm",
            "action": "kept going",
            "quote": "  yep\n next ",
        }
        item.update(over)
        return item

    def test_valid_event_is_enriched_from_its_pair(self):
        events, errors = classify.validate_events({"events": [self.good()]}, pairs(3), ROWS)
        self.assertEqual(errors, [])
        self.assertEqual(events[0]["prompt_id"], "p01")
        self.assertEqual(events[0]["ts"], "2026-09-06T00:00:01Z")
        self.assertEqual(events[0]["quote"], "yep next")

    def test_out_of_vocabulary_events_are_dropped_not_guessed(self):
        bad = [
            self.good(pair=7),
            self.good(pair="1"),
            self.good(type="praise"),
            self.good(polarity="neutral"),
            self.good(rule="Z9"),
            self.good(rule=None, section="mood"),
            self.good(action="  "),
            "not an object",
        ]
        events, errors = classify.validate_events({"events": bad}, pairs(3), ROWS)
        self.assertEqual(events, [])
        self.assertEqual(len(errors), len(bad))

    def test_rule_fixes_section_and_ids_are_normalised(self):
        events, errors = classify.validate_events(
            {"events": [self.good(rule=" o3 ", section="judgment")]}, pairs(3), ROWS
        )
        self.assertEqual(errors, [])
        self.assertEqual(events[0]["rule"], "O3")
        self.assertEqual(events[0]["section"], "output")

    def test_unattached_event_keeps_its_section(self):
        events, _ = classify.validate_events(
            {"events": [self.good(rule=None, section="anticipation")]}, pairs(3), ROWS
        )
        self.assertIsNone(events[0]["rule"])
        self.assertEqual(events[0]["section"], "anticipation")

    def test_missing_events_list_is_an_error(self):
        self.assertEqual(
            classify.validate_events({"nope": 1}, pairs(1), ROWS),
            ([], ["payload has no 'events' list"]),
        )

    def test_long_fields_are_truncated(self):
        events, _ = classify.validate_events(
            {"events": [self.good(action="a" * 500, quote="q" * 500)]}, pairs(3), ROWS
        )
        self.assertEqual(len(events[0]["action"]), classify.ACTION_MAX_CHARS)
        self.assertEqual(len(events[0]["quote"]), classify.QUOTE_MAX_CHARS)


class PromptTests(unittest.TestCase):
    def test_prompt_names_rules_and_numbers_pairs(self):
        prompt = classify.build_prompt(pairs(2), ROWS)
        self.assertIn("- J1 [judgment; principle] Prefer thorough.", prompt)
        self.assertIn("- O3 [output; specific of O1] Ask in prose.", prompt)
        self.assertIn("### pair 0", prompt)
        self.assertIn("### pair 1", prompt)
        self.assertIn("2 pairs follow.", prompt)
        for word in ("correction", "acceptance", "anticipation_miss", "confirm", "contradict"):
            self.assertIn(word, prompt)


class FakeClaude:
    def __init__(self, rc=0, events=None, stdout=None):
        self.rc = rc
        self.events = events if events is not None else []
        self.stdout = stdout
        self.calls = []

    def __call__(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.stdout is not None:
            return self.rc, self.stdout, ""
        return (
            self.rc,
            json.dumps({"result": json.dumps({"events": self.events})}),
            "boom" if self.rc else "",
        )


class SessionFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name).resolve() / "state"
        patcher = mock.patch.dict(os.environ, {store.TARGET_STATE_DIR_ENV: str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_turns(self, sid: str, n: int, *, age_hours: float = 0.0) -> Path:
        path = store.turns_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(p) + "\n" for p in pairs(n)), encoding="utf-8")
        if age_hours:
            stamp = time.time() - age_hours * 3600
            os.utime(path, (stamp, stamp))
        return path

    def test_classifies_writes_events_and_rebuilds_rules(self):
        self.write_turns(SID, 6)
        fake = FakeClaude(
            events=[
                {
                    "pair": 2,
                    "type": "acceptance",
                    "section": "judgment",
                    "rule": "J2",
                    "polarity": "confirm",
                    "action": "kept going",
                    "quote": "next",
                },
                {
                    "pair": 99,
                    "type": "acceptance",
                    "section": "judgment",
                    "rule": "J2",
                    "polarity": "confirm",
                    "action": "x",
                    "quote": "",
                },
            ]
        )
        rc = classify.classify_session(SID, run_claude=fake)
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1]["model"], classify.DEFAULT_MODEL)
        self.assertEqual(fake.calls[0][1]["max_turns"], 1)
        self.assertEqual(fake.calls[0][1]["allowed_tools"], ())
        data = json.loads(store.events_path(SID).read_text(encoding="utf-8"))
        self.assertEqual(data["pairs"], 6)
        self.assertEqual(data["dropped"], 1)
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["prompt_id"], "p02")
        self.assertIsNone(data["skipped"])
        text = store.rules_path().read_text(encoding="utf-8")
        self.assertIn("from 1 event(s) across 1 session(s)", text)
        self.assertIn("**J2**", text)

    def test_current_events_file_short_circuits_unless_forced(self):
        self.write_turns(SID, 6)
        fake = FakeClaude()
        self.assertEqual(classify.classify_session(SID, run_claude=fake), 0)
        self.assertEqual(classify.classify_session(SID, run_claude=fake), 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(classify.classify_session(SID, run_claude=fake, force=True), 0)
        self.assertEqual(len(fake.calls), 2)

    def test_too_few_pairs_is_recorded_without_a_model_call(self):
        self.write_turns(SID, 3)
        fake = FakeClaude()
        self.assertEqual(classify.classify_session(SID, run_claude=fake), 0)
        self.assertEqual(fake.calls, [])
        data = json.loads(store.events_path(SID).read_text(encoding="utf-8"))
        self.assertEqual(data["skipped"], "too_few_pairs")
        self.assertEqual(data["events"], [])
        self.assertTrue(store.rules_path().is_file())

    def test_model_failure_or_garbage_writes_nothing(self):
        self.write_turns(SID, 6)
        self.assertEqual(classify.classify_session(SID, run_claude=FakeClaude(rc=1)), 1)
        self.assertFalse(store.events_path(SID).exists())
        self.assertEqual(
            classify.classify_session(SID, run_claude=FakeClaude(stdout="not json")), 1
        )
        self.assertFalse(store.events_path(SID).exists())

    def test_no_turns_file_is_a_no_op(self):
        self.assertEqual(classify.classify_session(SID, run_claude=FakeClaude()), 0)
        self.assertFalse(store.events_path(SID).exists())

    def test_dry_run_prints_the_prompt_and_writes_nothing(self):
        self.write_turns(SID, 6)
        fake = FakeClaude()
        with mock.patch("sys.stdout") as out:
            self.assertEqual(classify.classify_session(SID, run_claude=fake, dry_run=True), 0)
        self.assertTrue(out.write.called)
        self.assertEqual(fake.calls, [])
        self.assertFalse(store.events_path(SID).exists())

    def test_pending_sessions_skips_fresh_and_classified(self):
        self.write_turns("aaaa0001-0000-0000-0000-000000000000", 6, age_hours=5)
        self.write_turns("aaaa0002-0000-0000-0000-000000000000", 6, age_hours=0.1)  # live
        self.write_turns("aaaa0003-0000-0000-0000-000000000000", 6, age_hours=5)
        self.write_turns("aaaa0004-0000-0000-0000-000000000000", 8, age_hours=5)
        classify._write_events("aaaa0003", pairs=6, events=[], model="sonnet")  # current
        classify._write_events("aaaa0004", pairs=6, events=[], model="sonnet")  # stale: 8 pairs now
        self.assertEqual(sorted(classify.pending_sessions()), ["aaaa0001", "aaaa0004"])

    def test_catch_up_classifies_each_pending_session(self):
        self.write_turns("aaaa0001-0000-0000-0000-000000000000", 6, age_hours=5)
        self.write_turns("aaaa0002-0000-0000-0000-000000000000", 6, age_hours=5)
        fake = FakeClaude()
        with mock.patch.object(
            classify, "classify_session", wraps=classify.classify_session
        ) as spy:
            with mock.patch("runner.claude_subprocess.run_claude", fake):
                self.assertEqual(classify.main(["--catch-up"]), 0)
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(classify.main(["--catch-up"]), 0)  # nothing pending now

    def test_main_requires_a_target(self):
        with self.assertRaises(SystemExit):
            classify.main([])


class VocabularyAgreementTests(unittest.TestCase):
    def test_classifier_vocabulary_matches_rules_module(self):
        self.assertEqual(set(rules.SECTIONS), {"judgment", "output", "anticipation"})
        self.assertEqual(set(rules.POLARITIES), {"confirm", "contradict"})
        self.assertEqual(set(rules.EVENT_TYPES), {"correction", "acceptance", "anticipation_miss"})
        seed = store.load_seed_rules()
        self.assertEqual({s["id"] for s in seed["sections"]}, set(rules.SECTIONS))
        for row in seed["rules"]:
            self.assertIn(row["section"], rules.SECTIONS)
            self.assertIn(row["kind"], rules.KINDS)
            if row["kind"] == "specific":
                self.assertIn(row["parent"], {r["id"] for r in seed["rules"]})


if __name__ == "__main__":
    unittest.main()
