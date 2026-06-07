"""Tests for gui.app bridge methods that have logic worth pinning down.

The bridge is mostly thin pass-throughs to the active Session; only the methods
with real validation/IO are exercised here.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui import app as gui_app
from gui.app import GuiBridge


class LogBubbleAnomalyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Nested path so we also verify parent-dir creation.
        self.log_path = Path(self._tmp.name) / "apiary_gui" / "bubble_anomalies.jsonl"
        patcher = mock.patch.object(gui_app, "BUBBLE_ANOMALY_LOG", self.log_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The method never touches the app, so a None app is fine.
        self.bridge = GuiBridge(None)  # type: ignore[arg-type]

    def _lines(self):
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def test_valid_payload_appends_jsonl_line(self):
        payload = {"cause": "premature_idle_teardown", "sessionId": "abc"}
        self.assertTrue(self.bridge.log_bubble_anomaly(json.dumps(payload)))
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), payload)

    def test_appends_rather_than_overwrites(self):
        self.assertTrue(self.bridge.log_bubble_anomaly(json.dumps({"cause": "arming_gap"})))
        self.assertTrue(self.bridge.log_bubble_anomaly(json.dumps({"cause": "unknown"})))
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["cause"], "arming_gap")
        self.assertEqual(json.loads(lines[1])["cause"], "unknown")

    def test_invalid_json_is_rejected_without_writing(self):
        self.assertFalse(self.bridge.log_bubble_anomaly("{ not json"))
        self.assertFalse(self.log_path.exists())

    def test_non_object_json_is_rejected(self):
        # Valid JSON but not an object → reject so the log stays a stream of dicts.
        self.assertFalse(self.bridge.log_bubble_anomaly("[1, 2, 3]"))
        self.assertFalse(self.bridge.log_bubble_anomaly('"a string"'))
        self.assertFalse(self.log_path.exists())

    def test_non_string_and_empty_input_rejected(self):
        self.assertFalse(self.bridge.log_bubble_anomaly(""))
        self.assertFalse(self.bridge.log_bubble_anomaly(None))  # type: ignore[arg-type]
        self.assertFalse(self.bridge.log_bubble_anomaly(123))  # type: ignore[arg-type]
        self.assertFalse(self.log_path.exists())


if __name__ == "__main__":
    unittest.main()
