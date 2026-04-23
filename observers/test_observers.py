#!/usr/bin/env python3
"""Tests for observer scripts — defense-in-depth tool-name checks and handler shape."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observer.adapter import ObservationEvent, RawPayload
from observers import dogfood, ue_llm_toolkit


def _event(tool_name: str, tool_input: dict | None = None) -> ObservationEvent:
    return ObservationEvent(
        schema_version=1,
        session_id="sess-1",
        hook_event_name="PostToolUse",
        tool_name=tool_name,
        tool_use_id="tu-1",
        transcript_path="",
        cwd="",
        permission_mode="default",
        raw=RawPayload(tool_input=tool_input, tool_response=None),
    )


class TestDogfoodObserver(unittest.TestCase):

    def test_rejects_non_grep_tool_returns_none(self):
        self.assertIsNone(dogfood.handle(_event("Read", {"path": "x"})))
        self.assertIsNone(dogfood.handle(_event("Bash", {"command": "ls"})))

    def test_accepts_grep_and_returns_summary_dict(self):
        result = dogfood.handle(_event("Grep", {"pattern": "foo", "path": "src"}))
        self.assertIsNotNone(result)
        self.assertEqual(result["pattern"], "foo")
        self.assertEqual(result["path"], "src")

    def test_grep_with_glob_instead_of_path(self):
        result = dogfood.handle(_event("Grep", {"pattern": "foo", "glob": "*.py"}))
        self.assertEqual(result["path"], "*.py")

    def test_handles_missing_tool_input(self):
        result = dogfood.handle(_event("Grep", None))
        self.assertIsNotNone(result)
        self.assertEqual(result["pattern"], "")
        self.assertEqual(result["path"], "")

    def test_truncates_very_long_pattern(self):
        long_pattern = "a" * 500
        result = dogfood.handle(_event("Grep", {"pattern": long_pattern}))
        self.assertLess(len(result["pattern"]), 100)


class TestUeLlmToolkitObserver(unittest.TestCase):

    def test_rejects_non_prefixed_tool_returns_none(self):
        self.assertIsNone(ue_llm_toolkit.handle(_event("Grep")))
        self.assertIsNone(ue_llm_toolkit.handle(_event("mcp__other_server__tool")))

    def test_accepts_matching_prefix_and_returns_summary_dict(self):
        result = ue_llm_toolkit.handle(
            _event(
                "mcp__ue_llm_toolkit__inspect_blueprint",
                {"blueprint": "BP_MainCharacter"},
            )
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "mcp__ue_llm_toolkit__inspect_blueprint")
        self.assertIn("blueprint=BP_MainCharacter", result["input_summary"])

    def test_summarises_without_known_keys(self):
        result = ue_llm_toolkit.handle(
            _event(
                "mcp__ue_llm_toolkit__get_stats",
                {"unknown_key": "v", "another": 2},
            )
        )
        self.assertIn("keys=[", result["input_summary"])

    def test_empty_tool_input_produces_no_input_summary(self):
        result = ue_llm_toolkit.handle(_event("mcp__ue_llm_toolkit__noop", {}))
        self.assertIn("(no input)", result["input_summary"])


if __name__ == "__main__":
    unittest.main()
