#!/usr/bin/env python3
"""Tests for scribe/infer.py and the retrotag walk.

The point of the module is that it is *off*: the regression this guards is
review §3 bug 10, where every `/wrapup` learning spawned a `claude -p` call
on the critical path. Nothing here ever runs a real subprocess — the tests
either assert no call was made, or patch the inference entry point.
"""

import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scribe.notes as notes_mod
from scribe import infer, maintenance
from scribe.store import ScribeStore


class InferenceSwitchTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(infer.inference_enabled(Namespace(), environ={}))

    def test_flag_turns_it_on(self):
        self.assertTrue(infer.inference_enabled(Namespace(infer=True, no_infer=False), environ={}))

    def test_env_turns_it_on(self):
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value):
                self.assertTrue(
                    infer.inference_enabled(Namespace(), environ={infer.INFER_ENV_VAR: value})
                )

    def test_env_junk_leaves_it_off(self):
        self.assertFalse(
            infer.inference_enabled(Namespace(), environ={infer.INFER_ENV_VAR: "maybe"})
        )

    def test_no_infer_beats_both(self):
        self.assertFalse(
            infer.inference_enabled(
                Namespace(infer=True, no_infer=True), environ={infer.INFER_ENV_VAR: "1"}
            )
        )


class ParseResponseTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            infer.parse_response('{"tags": ["a"], "areas": []}'), {"tags": ["a"], "areas": []}
        )

    def test_claude_envelope(self):
        envelope = '{"result": "{\\"tags\\": [\\"scribe\\"], \\"areas\\": []}"}'
        self.assertEqual(infer.parse_response(envelope)["tags"], ["scribe"])

    def test_markdown_fence(self):
        fenced = '```json\n{"tags": ["gui"], "areas": ["gui/**"]}\n```'
        self.assertEqual(infer.parse_response(fenced)["areas"], ["gui/**"])

    def test_junk_returns_none(self):
        for text in ("", "sure, here you go", "[1, 2, 3]"):
            with self.subTest(text=text):
                self.assertIsNone(infer.parse_response(text))

    def test_normalize_drops_blanks_and_stringifies(self):
        self.assertEqual(
            infer.normalize({"tags": ["  a  ", "", 7], "areas": None}),
            {"tags": ["a", "7"], "areas": []},
        )


class LearnDoesNotCallAModelTests(unittest.TestCase):
    """`/wrapup` writes learnings without --tags; that must cost nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.store = ScribeStore(self.tmp_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _learn_args(self, **overrides):
        base = dict(
            store=self.store,
            content="learned a thing",
            content_file=None,
            session_id="s1",
            brief_summary="",
            role="",
            mission="",
            tags="",
            area=[],
            supersedes="",
            infer=False,
            no_infer=False,
        )
        base.update(overrides)
        return Namespace(**base)

    def test_untagged_learn_makes_no_call(self):
        with mock.patch.object(infer, "infer_tags_areas") as called:
            notes_mod.cmd_learn(self._learn_args())
        called.assert_not_called()
        learnings = self.store.list_learnings()
        self.assertEqual(len(learnings), 1)
        self.assertEqual(learnings[0]["tags"], [])

    def test_infer_flag_makes_the_call(self):
        with mock.patch.object(
            infer, "infer_tags_areas", return_value={"tags": ["scribe"], "areas": ["scribe/**"]}
        ) as called:
            notes_mod.cmd_learn(self._learn_args(infer=True))
        called.assert_called_once()
        self.assertEqual(self.store.list_learnings()[0]["tags"], ["scribe"])

    def test_env_opt_in_makes_the_call(self):
        with (
            mock.patch.dict("os.environ", {infer.INFER_ENV_VAR: "1"}),
            mock.patch.object(
                infer, "infer_tags_areas", return_value={"tags": ["env"], "areas": []}
            ) as called,
        ):
            notes_mod.cmd_learn(self._learn_args())
        called.assert_called_once()

    def test_no_infer_beats_the_env(self):
        with (
            mock.patch.dict("os.environ", {infer.INFER_ENV_VAR: "1"}),
            mock.patch.object(infer, "infer_tags_areas") as called,
        ):
            notes_mod.cmd_learn(self._learn_args(no_infer=True))
        called.assert_not_called()

    def test_supplied_tags_are_never_overridden(self):
        with mock.patch.object(
            infer, "infer_tags_areas", return_value={"tags": ["guessed"], "areas": []}
        ) as called:
            notes_mod.cmd_learn(self._learn_args(infer=True, tags="mine"))
        called.assert_not_called()
        self.assertEqual(self.store.list_learnings()[0]["tags"], ["mine"])


class RetrotagTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.store = ScribeStore(self.tmp_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_infer(self, tags=("inferred",), areas=("scribe/**",)):
        return mock.patch.object(
            infer, "infer_tags_areas", return_value={"tags": list(tags), "areas": list(areas)}
        )

    def test_tags_an_untagged_learning_in_index_and_body(self):
        entry = self.store.add_learning("a bare learning", "s1")
        with self._fake_infer():
            report = maintenance.retrotag(self.store)
        self.assertEqual(report.processed, 1)
        got = self.store.get_learning(entry["year"], entry["seq"])
        self.assertEqual(got["tags"], ["inferred"])
        self.assertEqual(got["areas"], ["scribe/**"])
        self.assertEqual(got["content"], "a bare learning", "body must survive intact")
        body = (self.tmp_dir / "learnings" / str(entry["year"]) / f"{entry['seq']}.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("tags: [inferred]", body)

    def test_already_tagged_learnings_are_skipped(self):
        self.store.add_learning("tagged already", "s1", tags=["scribe"])
        with self._fake_infer() as called:
            report = maintenance.retrotag(self.store)
        called.assert_not_called()
        self.assertEqual((report.processed, report.already_tagged), (0, 1))

    def test_dry_run_writes_nothing(self):
        entry = self.store.add_learning("a bare learning", "s1")
        with self._fake_infer():
            report = maintenance.retrotag(self.store, dry_run=True)
        self.assertEqual(report.processed, 1)
        self.assertEqual(self.store.get_learning(entry["year"], entry["seq"])["tags"], [])

    def test_limit_caps_the_walk(self):
        for i in range(3):
            self.store.add_learning(f"learning {i}", "s1")
        with self._fake_infer():
            report = maintenance.retrotag(self.store, limit=2)
        self.assertEqual((report.total, report.processed), (2, 2))

    def test_a_failed_inference_is_recorded_not_raised(self):
        self.store.add_learning("a bare learning", "s1")
        with mock.patch.object(infer, "infer_tags_areas", return_value={}):
            report = maintenance.retrotag(self.store)
        self.assertEqual(report.processed, 0)

    def test_is_idempotent(self):
        self.store.add_learning("a bare learning", "s1")
        with self._fake_infer():
            maintenance.retrotag(self.store)
            second = maintenance.retrotag(self.store)
        self.assertEqual((second.processed, second.already_tagged), (0, 1))

    def test_cmd_retrotag_reports(self):
        import contextlib
        import io

        self.store.add_learning("a bare learning", "s1")
        out = io.StringIO()
        args = Namespace(store=self.store, dry_run=False, model=None, limit=None)
        with self._fake_infer(), contextlib.redirect_stdout(out):
            notes_mod.cmd_retrotag(args)
        self.assertIn("Retrotag complete: 1 tagged", out.getvalue())


if __name__ == "__main__":
    unittest.main()
