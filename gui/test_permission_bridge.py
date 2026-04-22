"""Unit tests for PermissionBridge.

Exercises the full loopback round-trip: boot the HTTP server, POST a
permission request, have the test's on_request callback resolve on a
background thread, verify the HTTP response carries the decision.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from gui.permission_bridge import PermissionBridge


def _post(url: str, payload: dict, timeout: float = 5.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class PermissionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.received: list[tuple[str, dict]] = []
        self.bridge: PermissionBridge | None = None

    def tearDown(self):
        if self.bridge is not None:
            self.bridge.stop()

    def _boot(self, decider=None, *, timeout_seconds: float = 5.0) -> str:
        def on_request(pid: str, payload: dict) -> None:
            self.received.append((pid, payload))
            if decider is None:
                return
            # Resolve on a bg thread so we don't block the handler.
            def worker():
                decision = decider(payload)
                self.bridge.resolve(pid, decision)
            threading.Thread(target=worker, daemon=True).start()

        self.bridge = PermissionBridge(on_request, timeout_seconds=timeout_seconds)
        return self.bridge.start()

    def test_roundtrip_allow_decision(self):
        url = self._boot(lambda payload: {"behavior": "allow", "updatedInput": payload.get("input", {})})
        status, body = _post(url, {"tool_name": "Edit", "input": {"file_path": "/x"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["behavior"], "allow")
        self.assertEqual(body["updatedInput"], {"file_path": "/x"})
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0][1]["tool_name"], "Edit")

    def test_roundtrip_deny_decision_with_message(self):
        url = self._boot(lambda p: {"behavior": "deny", "message": "nope"})
        _, body = _post(url, {"tool_name": "Bash", "input": {"command": "rm -rf /"}})
        self.assertEqual(body["behavior"], "deny")
        self.assertEqual(body["message"], "nope")

    def test_timeout_returns_deny(self):
        # No decider → the handler waits, hits timeout, returns deny.
        url = self._boot(decider=None, timeout_seconds=0.2)
        _, body = _post(url, {"tool_name": "Edit", "input": {}})
        self.assertEqual(body["behavior"], "deny")
        self.assertEqual(body["message"], "timeout")

    def test_404_on_wrong_path(self):
        url = self._boot(decider=None)
        wrong = url.replace("/permission", "/bogus")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _post(wrong, {})
        self.assertEqual(ctx.exception.code, 404)

    def test_400_on_invalid_json(self):
        url = self._boot(decider=None)
        req = urllib.request.Request(
            url, data=b"not json at all",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=2.0)
        self.assertEqual(ctx.exception.code, 400)

    def test_resolve_unknown_id_returns_false(self):
        self._boot(decider=None)
        self.assertFalse(self.bridge.resolve("does-not-exist", {"behavior": "allow"}))

    def test_stop_unblocks_pending(self):
        # A request that never gets resolved — stop() should unblock it
        # with a deny so the caller doesn't hang forever.
        url = self._boot(decider=None, timeout_seconds=30.0)

        result: dict = {}
        def caller():
            try:
                _, body = _post(url, {"tool_name": "Edit", "input": {}}, timeout=10.0)
                result["body"] = body
            except Exception as e:
                result["error"] = str(e)

        t = threading.Thread(target=caller, daemon=True)
        t.start()
        # Wait until the request lands in the registry.
        for _ in range(100):
            if self.bridge.pending_ids():
                break
            threading.Event().wait(0.02)
        self.assertTrue(self.bridge.pending_ids())
        self.bridge.stop()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("body", {}).get("behavior"), "deny")
        self.bridge = None  # stop() already ran, prevent tearDown re-stop


if __name__ == "__main__":
    unittest.main()
