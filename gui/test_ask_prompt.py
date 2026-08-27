"""Unit tests for gui.ask_prompt — the transcript-sourced AskUserQuestion watcher.

The record fixtures here are NOT hand-guessed shapes: they mirror records pulled
verbatim from a live transcript (the AskUserQuestion tool_use that the GUI failed
to scrape, and its rejection tool_result). See L-2026-133 — debug the real
artifact, not an invented fixture.
"""

import unittest

from gui.ask_prompt import (
    AskPromptWatcher,
    extract_ask_prompt,
    extract_resolved_ids,
)

TOOL_USE_ID = "toolu_01KThme4RF9etZtuFZCsNXvm"

# An assistant record carrying a text block followed by the AskUserQuestion
# tool_use — exactly the layout the live transcript showed.
ASK_RECORD = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I'll trigger a real AskUserQuestion."},
            {
                "type": "tool_use",
                "id": TOOL_USE_ID,
                "name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "Does the banner render this card?",
                            "header": "Banner test",
                            "multiSelect": False,
                            "options": [
                                {
                                    "label": "Renders perfectly",
                                    "description": "All options listed, no toast.",
                                },
                                {
                                    "label": "Card shows, no highlight",
                                    "description": "Border missing.",
                                },
                                {"label": "Truncated", "description": "Some options cut off."},
                                {"label": "Red fallback toast", "description": "The old failure."},
                            ],
                        }
                    ]
                },
            },
        ],
    },
}

# The matching tool_result lands in a user record. This is the real rejection
# payload (is_error True) — a cancel must still clear the prompt.
RESULT_RECORD = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": TOOL_USE_ID,
                "content": "The user doesn't want to proceed with this tool use.",
                "is_error": True,
            }
        ],
    },
}

# Ordinary assistant prose — must never look like a prompt.
PLAIN_RECORD = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Here are 1. and 2. items"}],
    },
}


class ExtractTests(unittest.TestCase):
    def test_extract_pulls_questions(self):
        p = extract_ask_prompt(ASK_RECORD)
        self.assertIsNotNone(p)
        self.assertEqual(p["tool_use_id"], TOOL_USE_ID)
        self.assertEqual(len(p["questions"]), 1)
        self.assertEqual(len(p["questions"][0]["options"]), 4)
        self.assertEqual(p["questions"][0]["options"][0]["label"], "Renders perfectly")

    def test_plain_record_is_not_a_prompt(self):
        self.assertIsNone(extract_ask_prompt(PLAIN_RECORD))
        self.assertIsNone(extract_ask_prompt(RESULT_RECORD))

    def test_resolved_ids(self):
        self.assertEqual(extract_resolved_ids(RESULT_RECORD), [TOOL_USE_ID])
        self.assertEqual(extract_resolved_ids(ASK_RECORD), [])

    def test_garbage_records_are_safe(self):
        for junk in [None, {}, {"type": "assistant"}, {"message": "x"}, 42]:
            self.assertIsNone(extract_ask_prompt(junk if isinstance(junk, dict) else {}))


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.prompts = []
        self.resolved = []
        self.w = AskPromptWatcher(self.prompts.append, self.resolved.append)

    def test_live_prompt_then_resolve(self):
        self.w.note_record(ASK_RECORD)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.prompts[0]["tool_use_id"], TOOL_USE_ID)
        self.assertEqual(self.resolved, [])
        self.w.note_record(RESULT_RECORD)
        self.assertEqual(self.resolved, [TOOL_USE_ID])

    def test_prompt_fires_once(self):
        self.w.note_record(ASK_RECORD)
        self.w.note_record(ASK_RECORD)  # duplicate poll/replay overlap
        self.assertEqual(len(self.prompts), 1)

    def test_resolve_without_pending_is_noop(self):
        self.w.note_record(RESULT_RECORD)
        self.assertEqual(self.resolved, [])

    def test_replay_suppresses_already_answered(self):
        # History where the prompt was asked AND answered before attach: no
        # banner should flash.
        self.w.replay([ASK_RECORD, RESULT_RECORD])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.resolved, [])

    def test_replay_surfaces_still_pending(self):
        # Asked but not yet answered at attach time: surface it once.
        self.w.replay([ASK_RECORD])
        self.assertEqual(len(self.prompts), 1)
        # And a later live resolve still clears it.
        self.w.note_record(RESULT_RECORD)
        self.assertEqual(self.resolved, [TOOL_USE_ID])


if __name__ == "__main__":
    unittest.main()
