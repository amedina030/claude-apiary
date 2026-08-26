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
server DENIES every request — a permission gate must never silently turn
into a rubber stamp (review C-2). Headless tests of the plumbing opt into
auto-allow explicitly with `APIARY_PERMISSION_MCP_ALLOW_ALL=1`.

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
# CLI flag the frozen GUI exe recognizes to dispatch into `serve()` instead
# of booting the webview. Used only in PyInstaller builds — dev-mode runs
# `python permission_mcp.py` directly and this flag is irrelevant.
MCP_SERVER_FLAG = "--mcp-server"

# Log and mcp-config live under the GUI's per-profile state dir
# (gui.paths.state_dir()) so profiles don't share one file and nothing lands
# in ~/.claude. Tests override these; None means "resolve from state_dir()".
LOG_PATH: Path | None = None
CONFIG_PATH: Path | None = None
LOG_FILE_NAME = "permission_mcp.log"
CONFIG_FILE_NAME = "permission_mcp_config.json"
# The log records every permission-gated tool input. Cap it: when it grows
# past this, the current file is rotated to ``<name>.1`` (one generation).
LOG_MAX_BYTES = 1024 * 1024
# Input keys whose values are never written to the log (file bodies, edit
# strings). Other string values are truncated to _LOG_STR_LIMIT chars.
_REDACT_KEYS = frozenset({"content", "old_string", "new_string"})
_LOG_STR_LIMIT = 200

# Env var the GUI process sets before spawning claude. Its value is the URL
# of the PermissionBridge's /permission endpoint (something like
# http://127.0.0.1:54123/permission). If unset, the server denies every
# request (fail closed).
BRIDGE_URL_ENV = "APIARY_PERMISSION_MCP_URL"
# Explicit opt-in to auto-allow when no bridge URL is set. For headless
# tests of the MCP plumbing only; the GUI never sets it.
ALLOW_ALL_ENV = "APIARY_PERMISSION_MCP_ALLOW_ALL"
# Per-tab env var: session.py sets this to the owning Session.session_id so
# the GUI can route an incoming prompt to the correct tab's banner instead
# of always landing it on the active tab. Empty/unset falls through to
# "no tab attribution" and the GUI treats the prompt as active-tab local.
SESSION_ID_ENV = "APIARY_SESSION_ID"
BRIDGE_HTTP_TIMEOUT_SECONDS = 300.0


def _state_dir() -> Path:
    try:
        from gui.paths import state_dir
    except ImportError:
        # Run as a bare script (``python gui/permission_mcp.py``, the
        # dev-mode mcp-config entry): the checkout root isn't on sys.path.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from gui.paths import state_dir
    return state_dir()


def log_path() -> Path:
    return LOG_PATH if LOG_PATH is not None else _state_dir() / LOG_FILE_NAME


def config_path() -> Path:
    return CONFIG_PATH if CONFIG_PATH is not None else _state_dir() / CONFIG_FILE_NAME


def permission_tool_arg() -> str:
    """Return the `mcp__<server>__<tool>` id for `--permission-prompt-tool`."""
    return f"mcp__{SERVER_NAME}__{TOOL_NAME}"


def mcp_enabled(launch: dict | None = None) -> bool:
    """Resolve whether the permission-prompt MCP path is on.

    `APIARY_PERMISSION_MCP` wins when set — `"1"` enables, anything else
    disables (so `APIARY_PERMISSION_MCP=0` explicitly overrides a launch.json
    that has the flag on). When the env var is unset, fall back to
    `launch["permission_mcp"]` (defaults to False).
    """
    env = os.environ.get("APIARY_PERMISSION_MCP")
    if env is not None:
        return env == "1"
    return bool((launch or {}).get("permission_mcp", False))


def write_mcp_config(
    dest: Path | None = None,
    *,
    python: str | None = None,
    script: Path | None = None,
    frozen: bool | None = None,
) -> Path:
    """Write a claude-code mcp-config file pointing at this server. Returns
    the destination path. `python` / `script` / `frozen` are test hooks;
    production callers pass none.

    In a PyInstaller bundle `sys.executable` is the frozen GUI exe, which
    can't run an arbitrary .py. The config instead invokes the exe with
    ``MCP_SERVER_FLAG`` so ``gui.app.main`` dispatches into ``serve()``
    rather than booting the webview.
    """
    dest_path = Path(dest) if dest is not None else config_path()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    command = python or sys.executable
    if is_frozen:
        args = [MCP_SERVER_FLAG]
    else:
        script_path = script if script is not None else Path(__file__)
        args = [str(Path(script_path).resolve())]
    config = {
        "mcpServers": {
            SERVER_NAME: {
                "command": command,
                "args": args,
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
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass  # a rotation that fails (file busy) must not drop the line
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def redact_for_log(value: Any, key: str = "") -> Any:
    """Return a copy of a tool-call payload safe to write to the log.

    File bodies and edit strings (``content``, ``old_string``,
    ``new_string``) are replaced by ``<redacted N chars>``; any other string
    longer than ``_LOG_STR_LIMIT`` is truncated. Structure is preserved so
    the log still shows which tool asked for what.
    """
    if isinstance(value, str):
        if key in _REDACT_KEYS:
            return f"<redacted {len(value)} chars>"
        if len(value) > _LOG_STR_LIMIT:
            return value[:_LOG_STR_LIMIT] + f"...<{len(value) - _LOG_STR_LIMIT} more chars>"
        return value
    if isinstance(value, dict):
        return {k: redact_for_log(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_for_log(v, key) for v in value]
    return value


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
    the payload there and return the GUI's decision. Otherwise DENY: a
    server reachable through a stale mcp-config, or one whose bridge failed
    to boot, must not rubber-stamp every call. Headless tests of the
    plumbing set `APIARY_PERMISSION_MCP_ALLOW_ALL=1` to auto-allow instead.

    The payload sent to the bridge is enriched with ``session_id`` (from
    ``APIARY_SESSION_ID``) so the GUI can route the prompt to the owning
    tab's banner instead of always landing on the active tab.
    """
    url = os.environ.get(BRIDGE_URL_ENV)
    if not url:
        if os.environ.get(ALLOW_ALL_ENV) == "1":
            _log(f"NO_BRIDGE {ALLOW_ALL_ENV}=1; auto-allowing")
            return _auto_allow(payload)
        _log("NO_BRIDGE denying (no bridge URL)")
        return {
            "behavior": "deny",
            "message": (
                "apiary permission bridge is not running "
                f"({BRIDGE_URL_ENV} unset); refusing to auto-approve"
            ),
        }
    enriched = dict(payload)
    session_id = os.environ.get(SESSION_ID_ENV, "")
    if session_id:
        enriched["session_id"] = session_id
    try:
        return _ask_bridge(url, enriched)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        _log(f"BRIDGE_ERR {e!r}; falling back to deny")
        return {"behavior": "deny", "message": f"permission bridge unreachable: {e}"}


def handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name != TOOL_NAME:
        decision = {"behavior": "deny", "message": f"unknown tool: {name!r}"}
    else:
        _log(f"REQUEST {json.dumps(redact_for_log(args), separators=(',', ':'))}")
        decision = decide(args)
        _log(f"DECISION {json.dumps(redact_for_log(decision), separators=(',', ':'))}")
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
