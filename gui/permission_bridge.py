"""Loopback HTTP bridge between the permission-prompt MCP subprocess and
the GUI process.

The MCP stdio server is spawned by claude-code, not by us — it has no
shared memory with the GUI. When claude needs a permission decision it
calls our MCP tool, whose body POSTs the request here; we surface it to
the frontend via a caller-supplied callback, wait for the human's
decision, and HTTP-respond with the MCP payload.

Single-process, loopback-only, no auth. Port 0 → OS picks a free port,
exposed via the `url` property. The URL is handed to the claude
subprocess through an env var so spawned MCP servers know where to POST.
"""

from __future__ import annotations

import http.server
import json
import threading
import uuid
from typing import Any, Callable, Optional

_DEFAULT_TIMEOUT_SECONDS = 300  # 5 min — longer than any sane human click


class PermissionBridge:
    """Owns the HTTP server + the pending-request registry.

    `on_request(pending_id, payload)` fires when a new permission request
    arrives. The callback is invoked on the HTTP handler thread; it must
    not block on the decision (that's what the registry + resolve() are
    for) — kick the UI update and return promptly.
    """

    def __init__(
        self,
        on_request: Callable[[str, dict[str, Any]], None],
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._on_request = on_request
        self._timeout = float(timeout_seconds)
        self._pending: dict[str, threading.Event] = {}
        self._decisions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: Optional[int] = None

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> str:
        """Bind to a free loopback port, start the server thread, return the
        URL the MCP server should POST to."""
        handler = self._make_handler()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server = server
        self._port = server.server_port
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="perm-bridge-http",
            daemon=True,
        )
        self._thread.start()
        return self.url  # type: ignore[return-value]

    def stop(self) -> None:
        """Tear down the server. Any still-pending requests get a deny so
        blocked MCP callers can unblock and the claude subprocess exits
        cleanly instead of hanging on shutdown."""
        with self._lock:
            for pid, evt in list(self._pending.items()):
                if pid not in self._decisions:
                    self._decisions[pid] = {
                        "behavior": "deny",
                        "message": "GUI shutting down",
                    }
                evt.set()
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        self._thread = None
        self._port = None

    @property
    def url(self) -> Optional[str]:
        if self._port is None:
            return None
        return f"http://127.0.0.1:{self._port}/permission"

    @property
    def port(self) -> Optional[int]:
        return self._port

    # --- decision api ---------------------------------------------------------

    def resolve(self, pending_id: str, decision: dict[str, Any]) -> bool:
        """Record the decision and wake the waiting handler. Returns False if
        the id is unknown (likely already timed out or resolved)."""
        with self._lock:
            evt = self._pending.get(pending_id)
            if evt is None:
                return False
            self._decisions[pending_id] = decision
            evt.set()
            return True

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending.keys())

    # --- internals ------------------------------------------------------------

    def _register_pending(self, pending_id: str, evt: threading.Event) -> None:
        with self._lock:
            self._pending[pending_id] = evt

    def _pop_pending(self, pending_id: str) -> dict[str, Any]:
        with self._lock:
            decision = self._decisions.pop(pending_id, None)
            self._pending.pop(pending_id, None)
        if decision is None:
            return {"behavior": "deny", "message": "timeout"}
        return decision

    def _make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
                if self.path != "/permission":
                    self.send_error(404, "not found")
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                try:
                    raw = self.rfile.read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    self.send_error(400, "invalid json")
                    return
                pending_id = str(uuid.uuid4())
                evt = threading.Event()
                bridge._register_pending(pending_id, evt)
                try:
                    bridge._on_request(pending_id, payload)
                except Exception as e:
                    bridge._pop_pending(pending_id)
                    self.send_error(500, f"dispatch failed: {e}")
                    return
                evt.wait(timeout=bridge._timeout)
                decision = bridge._pop_pending(pending_id)
                body = json.dumps(decision).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any, **kwargs: Any) -> None:
                # Suppress the default per-request stderr spam. The bridge
                # logs via callers if they want.
                pass

        return Handler
