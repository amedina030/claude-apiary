#!/usr/bin/env python3
"""Unit tests for research_capture_reminder — the pure decision + message.

The I/O shell (_run) reads stdin and touches a session flag file; the
behavior that matters (which tools fire, the once-per-session gate, and the
reminder content pointing at the researcher) lives in the pure helpers
``should_remind`` and ``reminder_text``, which are exercised here.
"""

import unittest

from core.hooks.research_capture_reminder import (
    RESEARCH_TOOLS,
    reminder_text,
    should_remind,
)


class ShouldRemindTest(unittest.TestCase):
    def test_fires_for_each_research_tool_when_not_yet_reminded(self):
        for tool in RESEARCH_TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(should_remind(tool, already_reminded=False))

    def test_covers_both_subagent_names(self):
        # Agent is this harness's subagent tool; Task is stock Claude Code's.
        self.assertIn("Agent", RESEARCH_TOOLS)
        self.assertIn("Task", RESEARCH_TOOLS)

    def test_silent_once_already_reminded(self):
        for tool in RESEARCH_TOOLS:
            with self.subTest(tool=tool):
                self.assertFalse(should_remind(tool, already_reminded=True))

    def test_ignores_unrelated_tools(self):
        for tool in ("Bash", "Edit", "Write", "Read", ""):
            with self.subTest(tool=tool):
                self.assertFalse(should_remind(tool, already_reminded=False))


class ReminderTextTest(unittest.TestCase):
    def test_points_at_the_researcher(self):
        text = reminder_text()
        self.assertIn("researcher", text)
        # Both the skill and the launcher invocation are offered.
        self.assertIn("/research add", text)
        self.assertIn("researcher/cli.py add", text)

    def test_scopes_to_durable_findings_not_every_search(self):
        text = reminder_text().lower()
        self.assertIn("durable", text)
        self.assertIn("throwaway", text)


if __name__ == "__main__":
    unittest.main()
