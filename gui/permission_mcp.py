"""Minimal MCP stdio server exposing a single `permission_prompt` tool.

Claude Code invokes this server when spawned with:

    claude --mcp-config <config.json> \
           --permission-prompt-tool mcp__apiary__permission_prompt

Every interactive approval that would otherwise surface as a TUI banner is
routed here instead, as a structured `{tool_use_id, tool_name, input}`
payload. The server returns `{behavior: "allow"|"deny", ...}` and claude
proceeds accordingly — no terminal scraping needed.

Decision flow: if the GUI exported a bridge URL via
`APIARY_PERMISSION_MCP_URL`, each request is POSTed there and the bridge
blocks on the human's click in the webview banner. Without that env the
server auto-allows — keeps unit tests and headless runs working.

Protocol: JSON-RPC 2.0 over newline-delimited JSON on stdio, MCP version
2024-11-05. We implement only the handful of methods claude-code calls.
Stdlib only so nothing new lands in the dependency graph.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "apiary"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "permission_prompt"

LOG_PATH = Path.home() / ".claude" / "apiary_gui" / "permission_mcp.log"
CONFIG_PATH = Path.home() / ".claude" / "apiary_gui" / "permission_mcp_config.json"

# Env var the GUI process sets before spawning claude. Its value is the URL
# of the PermissionBridge's /permission endpoint (something like
# http://127.0.0.1:54123/permission). If unset, the server falls back to
# auto-allowing every request — useful for headless testing of the MCP
# plumbing without a GUI attached.
BRIDGE_URL_ENV = "APIARY_PERMISSION_MCP_URL"
BRIDGE_HTTP_TIMEOUT_SECONDS = 300.0


def permission_tool_arg() -> str:
    """Return the `mcp__<server>__<tool>` id for `--permission-prompt-tool`."""
    return f"mcp__{SERVER_NAME}__{TOOL_NAME}"


def write_mcp_config(
    dest: Path | None = None,
    *,
    python: str | None = None,
    script: Path | None = None,
) -> Path:
    """Write a claude-code mcp-config file pointing at this server. Returns
    the destination path. `python` / `script` are test hooks; production
    callers pass neither."""
    dest_path = Path(dest) if dest is not None else CONFIG_PATH
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    script_path = script if script is not None else Path(__file__)
    config = {
        "mcpServers": {
            SERVER_NAME: {
                "command": python or sys.executable,
                "args": [str(Path(script_path).resolve())],
            }
        }
    }
    dest_path.write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest_path


def _log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def _send(msg: dict[str, Any], stream=sys.stdout) -> None:
    stream.write(json.dumps(msg, separators=(",", ":")) + "\n")
    stream.flush()


def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_initialize(_params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_tools_list(_params: dict[str, Any]) -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": TOOL_NAME,
                "description": (
                    "Approve or deny a tool call. Called by Claude Code when "
                    "--permission-prompt-tool points here."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string"},
                        "input": {"type": "object"},
                        "tool_use_id": {"type": "string"},
                    },
                    "required": ["tool_name", "input", "tool_use_id"],
                },
            }
        ]
    }


def _auto_allow(payload: dict[str, Any]) -> dict[str, Any]:
    return {"behavior": "allow", "updatedInput": payload.get("input", {})}


def _ask_bridge(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=BRIDGE_HTTP_TIMEOUT_SECONDS) as resp:
        raw = resp.read().decode("utf-8")
    decision = json.loads(raw)
    if not isinstance(decision, dict) or "behavior" not in decision:
        raise ValueError(f"bridge response missing 'behavior': {decision!r}")
    return decision


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve a permission request.

    If the GUI exported a bridge URL via `APIARY_PERMISSION_MCP_URL`, POST
    the payload there and return the GUI's decision. Otherwise auto-allow
    — keeps standalone/unit-test use working, and gives a safe default if
    the bridge fails to boot.
    """
    url = os.environ.get(BRIDGE_URL_ENV)
    if not url:
        return _auto_allow(payload)
    try:
        return _ask_bridge(url, payload)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        _log(f"BRIDGE_ERR {e!r}; falling back to deny")
        return {"behavior": "deny", "message": f"permission bridge unreachable: {e}"}


def handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name != TOOL_NAME:
        decision = {"behavior": "deny", "message": f"unknown tool: {name!r}"}
    else:
        _log(f"REQUEST {json.dumps(args, separators=(',', ':'))}")
        decision = decide(args)
        _log(f"DECISION {json.dumps(decision, separators=(',', ':'))}")
    return {"content": [{"type": "text", "text": json.dumps(decision)}]}


def dispatch(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Return a response dict, or None for notifications (no reply)."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = req_id is None

    if method == "initialize":
        return _result(req_id, handle_initialize(params))
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(req_id, handle_tools_list(params))
    if method == "tools/call":
        return _result(req_id, handle_tools_call(params))
    if is_notification:
        return None
    return _error(req_id, -32601, f"method not found: {method}")


def serve(stdin=sys.stdin, stdout=sys.stdout) -> None:
    _log(f"START pid={os.getpid()}")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"PARSE_ERR {e}")
            continue
        try:
            response = dispatch(msg)
        except Exception as e:
            _log(f"DISPATCH_ERR {e!r}")
            if msg.get("id") is not None:
                _send(_error(msg.get("id"), -32603, f"internal error: {e}"), stdout)
            continue
        if response is not None:
            _send(response, stdout)
    _log("EXIT")


if __name__ == "__main__":
    serve()
