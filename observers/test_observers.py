#!/usr/bin/env python3
"""Tests for observer scripts — defense-in-depth tool-name checks and handler shape."""
import sys
import unittest
from pathlib import Path
from unittest import mock

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

    def test_rejects_non_grep_tool_without_writing(self):
        with mock.patch.object(dogfood, "_write_scribe_reference") as writer:
            dogfood.handle(_event("Read", {"path": "x"}))
        writer.assert_not_called()

    def test_accepts_grep_and_writes_scribe_reference(self):
        with mock.patch.object(dogfood, "_write_scribe_reference") as writer:
            dogfood.handle(_event("Grep", {"pattern": "foo", "path": "src"}))
        writer.assert_called_once()
        summary, content = writer.call_args.args
        self.assertIn("Grep: foo", summary)
        self.assertIn("src", summary)
        self.assertIn("pattern", content)

    def test_handles_missing_tool_input(self):
        with mock.patch.object(dogfood, "_write_scribe_reference") as writer:
            dogfood.handle(_event("Grep", None))
        writer.assert_called_once()

    def test_truncates_very_long_pattern(self):
        long_pattern = "a" * 500
        with mock.patch.object(dogfood, "_write_scribe_reference") as writer:
            dogfood.handle(_event("Grep", {"pattern": long_pattern}))
        summary, _ = writer.call_args.args
        self.assertLess(len(summary), 200)


class TestUeLlmToolkitObserver(unittest.TestCase):

    def test_rejects_non_prefixed_tool_without_writing(self):
        with mock.patch.object(ue_llm_toolkit, "_write_scribe_reference") as writer:
            ue_llm_toolkit.handle(_event("Grep"))
            ue_llm_toolkit.handle(_event("mcp__other_server__tool"))
        writer.assert_not_called()

    def test_accepts_matching_prefix_and_writes_scribe_reference(self):
        with mock.patch.object(ue_llm_toolkit, "_write_scribe_reference") as writer:
            ue_llm_toolkit.handle(
                _event(
                    "mcp__ue_llm_toolkit__inspect_blueprint",
                    {"blueprint": "BP_MainCharacter"},
                )
            )
        writer.assert_called_once()
        summary, content = writer.call_args.args
        self.assertIn("mcp__ue_llm_toolkit__inspect_blueprint", summary)
        self.assertIn("blueprint=BP_MainCharacter", summary)
        self.assertIn("BP_MainCharacter", content)

    def test_summarises_without_known_keys(self):
        with mock.patch.object(ue_llm_toolkit, "_write_scribe_reference") as writer:
            ue_llm_toolkit.handle(
                _event(
                    "mcp__ue_llm_toolkit__get_stats",
                    {"unknown_key": "v", "another": 2},
                )
            )
        summary, _ = writer.call_args.args
        self.assertIn("keys=[", summary)

    def test_empty_tool_input_produces_no_input_summary(self):
        with mock.patch.object(ue_llm_toolkit, "_write_scribe_reference") as writer:
            ue_llm_toolkit.handle(
                _event("mcp__ue_llm_toolkit__noop", {})
            )
        summary, _ = writer.call_args.args
        self.assertIn("(no input)", summary)


if __name__ == "__main__":
    unittest.main()
