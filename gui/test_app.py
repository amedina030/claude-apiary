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


class StartPermissionBridgeTest(unittest.TestCase):
    """The MCP flag must never read "1" without a live bridge behind it
    (review C-2): a failed loopback bind used to leave APIARY_PERMISSION_MCP=1
    set with no URL, so claude was spawned with --permission-prompt-tool
    pointing at a server that auto-allowed everything."""

    ENV_KEYS = ("APIARY_PERMISSION_MCP", gui_app._PERMISSION_MCP_BRIDGE_URL_ENV)

    def setUp(self):
        import os

        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        import os

        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _app(self):
        app = gui_app.App.__new__(gui_app.App)
        app._permission_bridge = None
        app._push_permission_prompt = lambda *a, **k: None
        return app

    def test_bind_failure_pins_flag_off_and_leaves_no_url(self):
        import os

        class FailingBridge:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise OSError("address in use")

        app = self._app()
        with (
            mock.patch.object(gui_app, "load_launch", return_value={"permission_mcp": True}),
            mock.patch.object(gui_app, "PermissionBridge", FailingBridge),
        ):
            app._start_permission_bridge()
        self.assertIsNone(app._permission_bridge)
        self.assertEqual(os.environ.get("APIARY_PERMISSION_MCP"), "0")
        self.assertNotIn(gui_app._PERMISSION_MCP_BRIDGE_URL_ENV, os.environ)

    def test_successful_start_sets_url_then_flag(self):
        import os

        order = []

        class OkBridge:
            def __init__(self, *a, **k):
                pass

            def start(self):
                order.append(("flag_at_start", os.environ.get("APIARY_PERMISSION_MCP")))
                return "http://127.0.0.1:1/permission"

        app = self._app()
        with (
            mock.patch.object(gui_app, "load_launch", return_value={"permission_mcp": True}),
            mock.patch.object(gui_app, "PermissionBridge", OkBridge),
        ):
            app._start_permission_bridge()
        self.assertIsNotNone(app._permission_bridge)
        self.assertEqual(order, [("flag_at_start", None)])
        self.assertEqual(os.environ.get("APIARY_PERMISSION_MCP"), "1")
        self.assertEqual(
            os.environ.get(gui_app._PERMISSION_MCP_BRIDGE_URL_ENV),
            "http://127.0.0.1:1/permission",
        )

    def test_disabled_flag_does_nothing(self):
        import os

        app = self._app()
        with mock.patch.object(gui_app, "load_launch", return_value={"permission_mcp": False}):
            app._start_permission_bridge()
        self.assertIsNone(app._permission_bridge)
        self.assertNotIn("APIARY_PERMISSION_MCP", os.environ)


if __name__ == "__main__":
    unittest.main()
