#!/usr/bin/env python3
"""Tests for telephone/protocol.py: the preamble out, the reply back."""

import unittest

from telephone import protocol


class TestPreamble(unittest.TestCase):
    def _build(self, **kwargs):
        base = dict(
            caller="claude-apiary",
            callee="spanish-citizenship",
            mode="answer",
            call_id="C-2026-1",
            message="what documents did we submit",
        )
        base.update(kwargs)
        return protocol.build_preamble(**base)

    def test_names_both_sides_the_mode_and_the_call(self):
        text = self._build()
        self.assertIn("claude-apiary", text)
        self.assertIn("spanish-citizenship", text)
        self.assertIn("C-2026-1", text)
        self.assertIn("Mode: answer", text)
        self.assertIn("what documents did we submit", text)

    def test_answer_mode_forbids_edits_and_act_mode_names_the_branch(self):
        answer = self._build()
        self.assertIn("answer-mode call", answer)
        self.assertIn("Do not edit", answer)
        self.assertNotIn("Work branch", answer)

        act = self._build(mode="act", branch="telephone/C-2026-2")
        self.assertIn("act-mode call", act)
        self.assertIn("telephone/C-2026-2", act)
        self.assertIn("Never push", act)

    def test_every_preamble_forbids_a_nested_call(self):
        for mode in ("answer", "act"):
            self.assertIn("Do not place a telephone call of your own", self._build(mode=mode))

    def test_reply_format_lists_the_three_headings(self):
        text = self._build()
        for heading in protocol.SECTIONS:
            self.assertIn(f"## {heading}", text)

    def test_initiated_by_user_is_stated_differently_from_a_model_call(self):
        self.assertIn("the user", self._build(initiated_by="user"))
        self.assertIn("a Claude session working in", self._build(initiated_by="model"))

    def test_prior_exchanges_are_quoted_only_when_supplied(self):
        plain = self._build()
        self.assertNotIn("<exchange", plain)
        quoted = self._build(
            prior_exchanges=[{"number": "1", "message": "first ask", "reply": "first answer"}]
        )
        self.assertIn("could not be resumed", quoted)
        self.assertIn("first ask", quoted)
        self.assertIn("first answer", quoted)


class TestParseReply(unittest.TestCase):
    STRUCTURED = (
        "## Answer\n"
        "We submitted the birth certificate and the apostille.\n\n"
        "## Questions for the caller\n"
        "- Which year are you asking about?\n\n"
        "## Changes made\n"
        "none\n"
    )

    def test_splits_the_three_sections(self):
        parsed = protocol.parse_reply(self.STRUCTURED)
        self.assertTrue(parsed["structured"])
        self.assertIn("birth certificate", parsed["answer"])
        self.assertEqual(parsed["questions"], ["Which year are you asking about?"])
        self.assertEqual(parsed["changes"], [])

    def test_none_placeholders_are_dropped_from_the_lists(self):
        text = "## Answer\nok\n\n## Questions for the caller\nnone\n\n## Changes made\nNone.\n"
        parsed = protocol.parse_reply(text)
        self.assertEqual(parsed["questions"], [])
        self.assertEqual(parsed["changes"], [])

    def test_changes_are_listed_for_an_act_reply(self):
        text = "## Answer\ndone\n\n## Questions for the caller\nnone\n\n## Changes made\n- core/install.py\n- README.md\n"
        parsed = protocol.parse_reply(text)
        self.assertEqual(parsed["changes"], ["core/install.py", "README.md"])

    def test_an_unstructured_reply_still_reaches_the_caller(self):
        parsed = protocol.parse_reply("Just a plain sentence.")
        self.assertFalse(parsed["structured"])
        self.assertEqual(parsed["answer"], "Just a plain sentence.")
        self.assertEqual(parsed["questions"], [])

    def test_an_empty_reply_does_not_raise(self):
        for value in ("", None, "   "):
            parsed = protocol.parse_reply(value)
            self.assertEqual(parsed["questions"], [])


class TestFormatReply(unittest.TestCase):
    def test_status_line_carries_the_id_mode_and_who_placed_it(self):
        out = protocol.format_reply(
            protocol.parse_reply(TestParseReply.STRUCTURED),
            call_id="C-2026-1",
            mode="answer",
            initiated_by="user",
            status="answered",
        )
        self.assertIn("[telephone] C-2026-1 mode=answer initiated_by=user status=answered", out)
        self.assertIn("Which year are you asking about?", out)

    def test_empty_sections_are_not_printed(self):
        out = protocol.format_reply(
            {"answer": "hi", "questions": [], "changes": []},
            call_id="C-2026-2",
            mode="answer",
            initiated_by="model",
            status="answered",
        )
        self.assertNotIn(protocol.QUESTIONS_HEADING, out)
        self.assertNotIn(protocol.CHANGES_HEADING, out)


if __name__ == "__main__":
    unittest.main()
