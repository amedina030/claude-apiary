#!/usr/bin/env python3
"""Tests for observer.adapter — from_payload, from_stdin, AdapterError paths."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observer.adapter import (
    SCHEMA_VERSION,
    AdapterError,
    ObservationEvent,
    from_payload,
    from_stdin,
)


def _valid_payload(**overrides) -> dict:
    payload = {
        "session_id": "sess-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Grep",
        "tool_use_id": "tu-1",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/repo",
        "permission_mode": "default",
        "tool_input": {"pattern": "foo"},
        "tool_response": {"matches": 3},
    }
    payload.update(overrides)
    return payload


class TestFromPayload(unittest.TestCase):

    def test_complete_payload_produces_valid_event(self):
        event = from_payload(_valid_payload())
        self.assertEqual(event.schema_version, SCHEMA_VERSION)
        self.assertEqual(event.session_id, "sess-1")
        self.assertEqual(event.hook_event_name, "PostToolUse")
        self.assertEqual(event.tool_name, "Grep")
        self.assertEqual(event.tool_use_id, "tu-1")
        self.assertEqual(event.transcript_path, "/tmp/t.jsonl")
        self.assertEqual(event.cwd, "/repo")
        self.assertEqual(event.permission_mode, "default")
        self.assertEqual(event.raw.tool_input, {"pattern": "foo"})
        self.assertEqual(event.raw.tool_response, {"matches": 3})

    def test_missing_tool_response_sets_raw_to_none(self):
        payload = _valid_payload()
        del payload["tool_response"]
        event = from_payload(payload)
        self.assertIsNone(event.raw.tool_response)
        self.assertEqual(event.raw.tool_input, {"pattern": "foo"})

    def test_missing_tool_input_sets_raw_to_none(self):
        payload = _valid_payload()
        del payload["tool_input"]
        event = from_payload(payload)
        self.assertIsNone(event.raw.tool_input)

    def test_missing_hook_event_name_raises(self):
        payload = _valid_payload()
        del payload["hook_event_name"]
        with self.assertRaises(AdapterError):
            from_payload(payload)

    def test_missing_tool_name_raises(self):
        payload = _valid_payload()
        del payload["tool_name"]
        with self.assertRaises(AdapterError):
            from_payload(payload)

    def test_non_dict_payload_raises(self):
        with self.assertRaises(AdapterError):
            from_payload("not a dict")
        with self.assertRaises(AdapterError):
            from_payload(None)
        with self.assertRaises(AdapterError):
            from_payload([])

    def test_non_string_hook_event_name_raises(self):
        payload = _valid_payload(hook_event_name=42)
        with self.assertRaises(AdapterError):
            from_payload(payload)

    def test_optional_fields_default_to_empty_string(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Grep",
        }
        event = from_payload(payload)
        self.assertEqual(event.session_id, "")
        self.assertEqual(event.tool_use_id, "")
        self.assertEqual(event.transcript_path, "")
        self.assertEqual(event.cwd, "")
        self.assertEqual(event.permission_mode, "")

    def test_non_dict_tool_input_is_discarded(self):
        payload = _valid_payload(tool_input="garbage")
        event = from_payload(payload)
        self.assertIsNone(event.raw.tool_input)


class TestFromStdin(unittest.TestCase):

    def _patch_stdin(self, data: bytes):
        fake = io.BytesIO(data)
        return mock.patch.object(sys, "stdin", mock.Mock(buffer=fake))

    def test_valid_json_on_stdin_returns_event(self):
        payload = _valid_payload()
        with self._patch_stdin(json.dumps(payload).encode("utf-8")):
            event = from_stdin()
        self.assertIsInstance(event, ObservationEvent)
        self.assertEqual(event.tool_name, "Grep")

    def test_empty_stdin_raises(self):
        with self._patch_stdin(b""):
            with self.assertRaises(AdapterError):
                from_stdin()

    def test_malformed_json_raises(self):
        with self._patch_stdin(b"{not json"):
            with self.assertRaises(AdapterError):
                from_stdin()


if __name__ == "__main__":
    unittest.main()
