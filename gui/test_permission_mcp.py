"""Unit tests for the permission-prompt MCP stdio server.

Exercises the handful of JSON-RPC methods claude-code actually calls on
spawn: `initialize`, `tools/list`, and `tools/call`. The server writes to
a log file as a side effect; tests redirect it to a tempdir.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path


@contextlib.contextmanager
def _env(overrides: dict[str, str | None]):
    """Set/unset env vars for the duration of a block; restore originals."""
    originals = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in originals.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class PermissionMcpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Reimport module with a redirected log path so we don't touch the
        # user's real ~/.claude/.
        import gui.permission_mcp as mod

        importlib.reload(mod)
        mod.LOG_PATH = Path(self._tmp.name).resolve() / "permission_mcp.log"
        self.mod = mod

    def _roundtrip(self, messages: list[dict]) -> list[dict]:
        """Feed messages through serve() on fake stdin/stdout."""
        stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
        stdout = io.StringIO()
        self.mod.serve(stdin=stdin, stdout=stdout)
        out = stdout.getvalue().strip().splitlines()
        return [json.loads(line) for line in out if line.strip()]

    def test_initialize_returns_capabilities(self):
        responses = self._roundtrip(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            ]
        )
        self.assertEqual(len(responses), 1)
        r = responses[0]
        self.assertEqual(r["id"], 1)
        self.assertEqual(r["result"]["protocolVersion"], self.mod.PROTOCOL_VERSION)
        self.assertEqual(r["result"]["serverInfo"]["name"], self.mod.SERVER_NAME)
        self.assertIn("tools", r["result"]["capabilities"])

    def test_tools_list_exposes_permission_prompt(self):
        responses = self._roundtrip(
            [
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )
        self.assertEqual(len(responses), 1)
        tools = responses[0]["result"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], self.mod.TOOL_NAME)
        schema = tools[0]["inputSchema"]
        self.assertEqual(
            set(schema["required"]),
            {"tool_name", "input", "tool_use_id"},
        )

    def test_tools_call_without_bridge_denies(self):
        # No bridge URL in env → fail closed. A server reachable through a
        # stale mcp-config, or one whose bridge failed to boot, must not
        # rubber-stamp every call (review C-2).
        args = {
            "tool_use_id": "toolu_test",
            "tool_name": "Edit",
            "input": {"file_path": "/x", "old_string": "a", "new_string": "b"},
        }
        with _env({self.mod.BRIDGE_URL_ENV: None, self.mod.ALLOW_ALL_ENV: None}):
            responses = self._roundtrip(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": self.mod.TOOL_NAME, "arguments": args},
                    },
                ]
            )
        self.assertEqual(len(responses), 1)
        content = responses[0]["result"]["content"]
        self.assertEqual(content[0]["type"], "text")
        decision = json.loads(content[0]["text"])
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("bridge", decision["message"].lower())
        self.assertNotIn("updatedInput", decision)

    def test_allow_all_flag_is_the_only_way_to_auto_allow(self):
        args = {"tool_use_id": "t", "tool_name": "Bash", "input": {"command": "ls"}}
        with _env({self.mod.BRIDGE_URL_ENV: None, self.mod.ALLOW_ALL_ENV: "1"}):
            decision = self.mod.decide(args)
        self.assertEqual(decision["behavior"], "allow")
        self.assertEqual(decision["updatedInput"], args["input"])
        # Any other value is not an opt-in.
        for value in ("0", "true", "yes", ""):
            with self.subTest(value=value):
                with _env({self.mod.BRIDGE_URL_ENV: None, self.mod.ALLOW_ALL_ENV: value}):
                    self.assertEqual(self.mod.decide(args)["behavior"], "deny")

    def test_allow_all_flag_does_not_bypass_a_live_bridge(self):
        from gui.permission_bridge import PermissionBridge

        def on_request(pid, payload):
            bridge.resolve(pid, {"behavior": "deny", "message": "policy"})

        bridge = PermissionBridge(on_request, timeout_seconds=5.0)
        url = bridge.start()
        try:
            with _env({self.mod.BRIDGE_URL_ENV: url, self.mod.ALLOW_ALL_ENV: "1"}):
                decision = self.mod.decide(
                    {"tool_use_id": "t1", "tool_name": "Bash", "input": {"command": "ls"}}
                )
        finally:
            bridge.stop()
        self.assertEqual(decision["behavior"], "deny")

    def test_request_log_redacts_file_bodies_and_edit_strings(self):
        args = {
            "tool_use_id": "toolu_w",
            "tool_name": "Write",
            "input": {
                "file_path": "/x/secrets.env",
                "content": "AWS_SECRET_ACCESS_KEY=abcd1234efgh5678ijkl9012mnop3456qrst7890",  # apiary:allow-secret
                "old_string": "old",
                "new_string": "new",
            },
        }
        with _env({self.mod.BRIDGE_URL_ENV: None, self.mod.ALLOW_ALL_ENV: None}):
            self._roundtrip(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": self.mod.TOOL_NAME, "arguments": args},
                    },
                ]
            )
        log = self.mod.LOG_PATH.read_text(encoding="utf-8")
        self.assertIn("REQUEST", log)
        self.assertIn("/x/secrets.env", log)  # structure survives
        self.assertNotIn("abcd1234efgh5678", log)
        self.assertNotIn('"old"', log)
        self.assertIn("<redacted", log)

    def test_decision_log_is_redacted_too(self):
        # An Allow carries updatedInput with the full Write body; the log must
        # not keep it (the REQUEST line was redacted, the DECISION line was not).
        args = {
            "tool_use_id": "t",
            "tool_name": "Write",
            "input": {
                "file_path": "/x/secrets.env",
                "content": "TOKEN=zyxw9876vuts5432",  # apiary:allow-secret
            },
        }
        with _env({self.mod.BRIDGE_URL_ENV: None, self.mod.ALLOW_ALL_ENV: "1"}):
            self._roundtrip(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {"name": self.mod.TOOL_NAME, "arguments": args},
                    },
                ]
            )
        log = self.mod.LOG_PATH.read_text(encoding="utf-8")
        self.assertIn("DECISION", log)
        self.assertNotIn("zyxw9876", log)

    def test_redact_for_log_truncates_long_strings_and_keeps_shape(self):
        long = "x" * 500
        out = self.mod.redact_for_log(
            {
                "tool_name": "Bash",
                "input": {"command": long, "nested": [{"content": "body"}]},
            }
        )
        self.assertEqual(out["tool_name"], "Bash")
        self.assertLess(len(out["input"]["command"]), 300)
        self.assertIn("more chars", out["input"]["command"])
        self.assertEqual(out["input"]["nested"][0]["content"], "<redacted 4 chars>")

    def test_log_rotates_past_cap(self):
        self.mod.LOG_MAX_BYTES = 100
        self.mod.LOG_PATH.write_text("y" * 150, encoding="utf-8")
        self.mod._log("after rotation")
        rotated = self.mod.LOG_PATH.with_name(self.mod.LOG_PATH.name + ".1")
        self.assertTrue(rotated.exists())
        self.assertEqual(rotated.read_text(encoding="utf-8"), "y" * 150)
        self.assertIn("after rotation", self.mod.LOG_PATH.read_text(encoding="utf-8"))

    def test_paths_default_under_gui_state_dir_not_home(self):
        # The point of the assertion is *where the code puts state*, not what
        # the checkout happens to be called: assert containment in
        # gui.paths.state_dir() and absence from ~/.claude. A literal
        # ".claude not in parts" check fails in a worktree under .claude/
        # (T-2026-274).
        from gui.paths import state_dir

        self.mod.LOG_PATH = None
        self.mod.CONFIG_PATH = None
        sd = state_dir()
        self.assertEqual(self.mod.log_path(), sd / self.mod.LOG_FILE_NAME)
        self.assertEqual(self.mod.config_path(), sd / self.mod.CONFIG_FILE_NAME)
        self.assertTrue(self.mod.log_path().is_relative_to(sd))
        home_claude = Path.home() / ".claude"
        self.assertFalse(self.mod.log_path().is_relative_to(home_claude))
        self.assertFalse(self.mod.config_path().is_relative_to(home_claude))
        self.mod.LOG_PATH = Path(self._tmp.name).resolve() / "permission_mcp.log"

    def test_write_mcp_config_default_dest_is_config_path(self):
        self.mod.CONFIG_PATH = Path(self._tmp.name).resolve() / "cfg" / "mcp.json"
        dest = self.mod.write_mcp_config(python="/fake/python", script=Path("/fake/s.py"))
        self.assertEqual(dest, self.mod.CONFIG_PATH)
        self.assertTrue(dest.exists())

    def test_decide_with_bridge_forwards_and_parses(self):
        # Stand up a throwaway loopback bridge; verify decide() POSTs there
        # and surfaces the decision verbatim.
        from gui.permission_bridge import PermissionBridge

        def on_request(pid, payload):
            bridge.resolve(pid, {"behavior": "deny", "message": "policy"})

        bridge = PermissionBridge(on_request, timeout_seconds=5.0)
        url = bridge.start()
        try:
            os.environ[self.mod.BRIDGE_URL_ENV] = url
            decision = self.mod.decide(
                {
                    "tool_use_id": "t1",
                    "tool_name": "Bash",
                    "input": {"command": "rm -rf /"},
                }
            )
        finally:
            os.environ.pop(self.mod.BRIDGE_URL_ENV, None)
            bridge.stop()
        self.assertEqual(decision["behavior"], "deny")
        self.assertEqual(decision["message"], "policy")

    def test_decide_enriches_payload_with_session_id(self):
        # When APIARY_SESSION_ID is set, decide() should forward it in the
        # POST body so the GUI can route the prompt to the owning tab.
        from gui.permission_bridge import PermissionBridge

        captured: list[dict] = []

        def on_request(pid, payload):
            captured.append(payload)
            bridge.resolve(pid, {"behavior": "allow", "updatedInput": {}})

        bridge = PermissionBridge(on_request, timeout_seconds=5.0)
        url = bridge.start()
        try:
            os.environ[self.mod.BRIDGE_URL_ENV] = url
            os.environ[self.mod.SESSION_ID_ENV] = "sess-abc123"
            self.mod.decide(
                {
                    "tool_use_id": "t1",
                    "tool_name": "Bash",
                    "input": {"command": "ls"},
                }
            )
        finally:
            os.environ.pop(self.mod.BRIDGE_URL_ENV, None)
            os.environ.pop(self.mod.SESSION_ID_ENV, None)
            bridge.stop()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get("session_id"), "sess-abc123")

    def test_decide_denies_when_bridge_unreachable(self):
        # Point the env var at a closed port → decide() returns a deny.
        os.environ[self.mod.BRIDGE_URL_ENV] = "http://127.0.0.1:1/permission"
        try:
            decision = self.mod.decide(
                {
                    "tool_use_id": "t1",
                    "tool_name": "Edit",
                    "input": {},
                }
            )
        finally:
            os.environ.pop(self.mod.BRIDGE_URL_ENV, None)
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("bridge", decision["message"].lower())

    def test_tools_call_unknown_tool_denies(self):
        responses = self._roundtrip(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "bogus", "arguments": {}},
                },
            ]
        )
        decision = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(decision["behavior"], "deny")

    def test_notification_produces_no_response(self):
        responses = self._roundtrip(
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            ]
        )
        self.assertEqual(responses, [])

    def test_unknown_method_returns_error(self):
        responses = self._roundtrip(
            [
                {"jsonrpc": "2.0", "id": 5, "method": "completely/made_up"},
            ]
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32601)

    def test_write_mcp_config_shape(self):
        dest = Path(self._tmp.name).resolve() / "mcp.json"
        self.mod.write_mcp_config(
            dest=dest,
            python="/fake/python",
            script=Path("/fake/script.py"),
        )
        data = json.loads(dest.read_text(encoding="utf-8"))
        servers = data["mcpServers"]
        self.assertIn(self.mod.SERVER_NAME, servers)
        entry = servers[self.mod.SERVER_NAME]
        self.assertEqual(entry["command"], "/fake/python")
        self.assertEqual(len(entry["args"]), 1)
        self.assertTrue(entry["args"][0].endswith("script.py"))

    def test_write_mcp_config_frozen_uses_mcp_server_flag(self):
        # In a PyInstaller bundle sys.executable is the frozen GUI exe, which
        # can't run an arbitrary .py. Config must invoke the exe with
        # MCP_SERVER_FLAG so the bundled main() routes into serve().
        dest = Path(self._tmp.name).resolve() / "mcp.json"
        self.mod.write_mcp_config(
            dest=dest,
            python="C:/dist/apiary-gui/apiary-gui.exe",
            frozen=True,
        )
        entry = json.loads(dest.read_text(encoding="utf-8"))["mcpServers"][self.mod.SERVER_NAME]
        self.assertEqual(entry["command"], "C:/dist/apiary-gui/apiary-gui.exe")
        self.assertEqual(entry["args"], [self.mod.MCP_SERVER_FLAG])

    def test_permission_tool_arg_format(self):
        arg = self.mod.permission_tool_arg()
        self.assertEqual(
            arg,
            f"mcp__{self.mod.SERVER_NAME}__{self.mod.TOOL_NAME}",
        )

    def test_mcp_enabled_env_var_on(self):
        with _env({"APIARY_PERMISSION_MCP": "1"}):
            self.assertTrue(self.mod.mcp_enabled({}))
            self.assertTrue(self.mod.mcp_enabled({"permission_mcp": False}))

    def test_mcp_enabled_env_var_explicit_off_overrides_launch(self):
        with _env({"APIARY_PERMISSION_MCP": "0"}):
            self.assertFalse(self.mod.mcp_enabled({"permission_mcp": True}))

    def test_mcp_enabled_env_unset_falls_back_to_launch(self):
        with _env({"APIARY_PERMISSION_MCP": None}):
            self.assertFalse(self.mod.mcp_enabled({}))
            self.assertFalse(self.mod.mcp_enabled({"permission_mcp": False}))
            self.assertTrue(self.mod.mcp_enabled({"permission_mcp": True}))

    def test_mcp_enabled_no_args_defaults_to_off(self):
        with _env({"APIARY_PERMISSION_MCP": None}):
            self.assertFalse(self.mod.mcp_enabled())
            self.assertFalse(self.mod.mcp_enabled(None))

    def test_bad_json_is_skipped_not_fatal(self):
        # Feed one garbage line between two valid messages. The server
        # should log-and-continue, not abort.
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            + "\n"
            + "not-json-at-all\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        )
        stdout = io.StringIO()
        self.mod.serve(stdin=stdin, stdout=stdout)
        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        ids = [json.loads(ln)["id"] for ln in lines]
        self.assertEqual(ids, [1, 2])


if __name__ == "__main__":
    unittest.main()
