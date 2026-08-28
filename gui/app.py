"""apiary GUI entry point — `python -m gui.app`.

Spawns a PyWebView window and owns a list of Sessions — one per open tab.
Each Session bundles its own claude pty subprocess, transcript tail, and
session-discovery; App routes bridge calls and UI pushes to the active one.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from gui import (
    build_info,
    composer_state,
    file_refs,
    permission_mcp,
    picker,
    pty_capture,
    sidebar_state,
    tabs_state,
    usage_fetcher,
)
from gui.paths import main_apiary, state_dir
from gui.permission_bridge import PermissionBridge
from gui.permission_mcp import BRIDGE_URL_ENV as _PERMISSION_MCP_BRIDGE_URL_ENV
from gui.scribe_aggregator import NoteEntry, read_body
from gui.session import Session
from gui.single_instance import SingleInstance
from gui.tabs_state import TabEntry
from gui.theme import (
    ThemeWatcher,
    load_launch,
    load_theme,
)
from gui.theme import (
    ensure_defaults as ensure_theme_defaults,
)
from gui.transcript import Message, parse_jsonl_lines
from scribe import api as scribe_api

# Note types the quick-capture box may write. Deliberately narrow: the box is
# a low-friction inbox, not a general note editor. `wishlist` is the default
# because a mid-run thought is not yet a commitment, and because only
# todo/wishlist/blocker/context surface in the startup banner — a type outside
# that set would file the thought somewhere it is never seen again.
QUICK_NOTE_TYPES = ("wishlist", "todo")
from gui.win_notify import flash_taskbar
from gui.win_titlebar import apply_dark_titlebar, find_window_by_title


def _web_dir() -> Path:
    # PyInstaller-frozen builds put the entry script at the bundle root, so
    # Path(__file__).parent points at _internal/, not _internal/gui/. The spec
    # bundles web assets under _internal/gui/web/ — sys._MEIPASS is the bundle
    # root in both one-folder and one-file modes.
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "gui" / "web"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent / "web"


WEB_DIR = _web_dir()
INDEX_HTML = WEB_DIR / "index.html"

# Persistent diagnostic log for the thinking-bubble anomaly monitor. The
# frontend (bubble_monitor.js) detects when the bubble is wrongly absent and
# posts a classified snapshot here for later analysis — the bug is intermittent
# and unreproducible on demand, so we capture occurrences as they happen rather
# than guess at the cause. Same home-dir location convention as permission_mcp.
# Under the per-profile GUI state dir like every other GUI file — this was
# the last apiary writer under ~/.claude (review gui #5 / S1).
BUBBLE_ANOMALY_LOG = state_dir() / "bubble_anomalies.jsonl"


class GuiBridge:
    """JS → Python surface (frontend calls `pywebview.api.<method>()`).

    Every pty-touching method routes to ``app.active`` — the Session for the
    currently-selected tab. Returns False when there is no active session.
    """

    def __init__(self, app: "App") -> None:
        self._app = app

    def send_input(self, text: str) -> bool:
        sess = self._app.active
        return sess.send_input(text) if sess is not None else False

    def send_control(self, ch: str) -> bool:
        sess = self._app.active
        return sess.send_control(ch) if sess is not None else False

    def send_escape(self) -> bool:
        sess = self._app.active
        return sess.send_escape() if sess is not None else False

    def send_text(self, text: str) -> bool:
        sess = self._app.active
        return sess.send_text(text) if sess is not None else False

    def send_bytes(self, values) -> bool:
        """Send an exact byte sequence (list of ints 0-255) to the active pty.

        The arrow-key menu driver uses this for precise control sequences, e.g.
        arrow-up = [27, 91, 65]. Distinct from send_text (text/IME path) and
        send_control (single ctrl char).
        """
        sess = self._app.active
        return sess.send_bytes(values) if sess is not None else False

    def pty_resize(self, rows, cols) -> bool:
        sess = self._app.active
        if sess is None:
            return False
        try:
            r = int(rows)
            c = int(cols)
        except (TypeError, ValueError):
            return False
        if r <= 0 or c <= 0:
            return False
        return sess.pty_resize(r, c)

    def log_bubble_anomaly(self, payload: str) -> bool:
        """Append one thinking-bubble anomaly record (JSON string from the
        frontend monitor) to BUBBLE_ANOMALY_LOG as a JSONL line.

        Best-effort and never raises into the bridge: a diagnostic write must
        not be able to disrupt the UI. Validates that the payload is a JSON
        object before writing so the log stays parseable.
        """
        if not isinstance(payload, str) or not payload:
            return False
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return False
        if not isinstance(obj, dict):
            return False
        try:
            BUBBLE_ANOMALY_LOG.parent.mkdir(parents=True, exist_ok=True)
            with BUBBLE_ANOMALY_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        except OSError as e:
            print(f"[gui] log_bubble_anomaly failed: {e}", file=sys.stderr)
            return False
        return True

    # Drag-dropped files are added by the native pywebview drop handler
    # (App._on_file_drop), not by the frontend — the webview hands the real
    # path to Python, so JS never sees bytes. These bridge methods cover the
    # frontend-initiated actions: removing a chip, clearing, pasting, the
    # initial render, and the send-time manifest. All operate on the ACTIVE
    # tab's registry (Session.file_refs) — the panel only ever shows the active
    # tab, and a send always comes from it — so staged files never cross tabs.

    def remove_file(self, file_id: str) -> list[dict]:
        """Drop one file reference (user clicked its ✕); returns the new list.
        An owned (pasted) temp file is deleted; a dropped reference's target is
        never touched — we only hold a pointer."""
        s = self._app.active
        if s is None:
            return []
        s.file_refs.remove(file_id)
        return s.file_refs.list()

    def clear_files(self) -> bool:
        """Drop every file reference for the active tab."""
        s = self._app.active
        return s.file_refs.clear() if s is not None else False

    def list_files(self) -> list[dict]:
        """The active tab's file references (for the initial render / reload)."""
        s = self._app.active
        return s.file_refs.list() if s is not None else []

    def add_pasted_image(self, data_b64: str, mime: str = "", name: str = "") -> list[dict]:
        """Frontend pasted an image into the composer. A clipboard bitmap has no
        source path, so — unlike a drop — Python materializes the bytes into an
        owned temp file and registers it on the active tab. ``data_b64`` is the
        base64 payload of the image (no data-URL prefix). Returns the refreshed
        list so the panel renders the new row in one hop; bad input leaves the
        list unchanged."""
        s = self._app.active
        if s is None:
            return []
        if not isinstance(data_b64, str) or not data_b64:
            return s.file_refs.list()
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (ValueError, TypeError):
            return s.file_refs.list()
        s.file_refs.add_pasted_bytes(raw, mime or None, name or None)
        return s.file_refs.list()

    def manifest_and_mark(self) -> dict:
        """Build the active tab's send-time attach manifest authoritatively and
        mark the listed files shared. Called by the frontend the moment the user
        sends: existence is re-checked here (not from a stale JS cache), so a
        reference whose target vanished since it was dropped is dropped from the
        manifest instead of shipping a dead path. Returns {text, files} so the
        frontend appends the text and re-renders the panel in one hop."""
        s = self._app.active
        if s is None:
            return {"text": "", "files": []}
        return s.file_refs.manifest_and_mark()

    def get_note_body(self, body_path: str) -> str:
        """Frontend calls this when a sidebar note is clicked. Read-only — no scribe writes."""
        if not isinstance(body_path, str):
            return ""
        return read_body(body_path)

    def add_quick_note(self, text: str, note_type: str = "wishlist") -> dict:
        """Quick-capture a note into the ACTIVE tab's repo (#T-2026-251).

        Writes straight to that repo's scribe store. It deliberately never
        touches the pty, so jotting a thought mid-turn cannot interrupt,
        cancel, or race whatever claude is doing — which is the entire point
        of the feature, and keeps it clear of the never-kill-the-session rule.

        Returns ``{"ok", "display_id", "error"}``. The frontend keeps the
        typed text on failure rather than clearing optimistically: losing a
        thought to a backend hiccup is the one outcome that would make the
        feature untrustworthy.
        """
        body = text.strip() if isinstance(text, str) else ""
        if not body:
            return {"ok": False, "display_id": "", "error": "nothing to save"}
        if note_type not in QUICK_NOTE_TYPES:
            return {"ok": False, "display_id": "", "error": f"unsupported note type: {note_type}"}
        session = self._app.active
        if session is None:
            return {"ok": False, "display_id": "", "error": "no active tab"}
        try:
            # Pass main-apiary explicitly: scribe's own fallback derives it
            # from __file__, which lands inside the bundle in a frozen build
            # (cf. T-2026-248). gui.paths.main_apiary() is source/frozen aware.
            store = scribe_api.open_store(session.cwd, apiary_repo=main_apiary())
            entry = store.add_note(note_type, body, session.session_id[:8])
        except Exception as exc:  # noqa: BLE001 - surface, never crash the GUI
            return {"ok": False, "display_id": "", "error": f"{exc.__class__.__name__}: {exc}"}
        # Show it now instead of up to one aggregator tick (~5s) later.
        try:
            session.flush_notes()
        except Exception as exc:  # noqa: BLE001 - the note is already written
            print(f"[gui] quick-note resync failed: {exc}", file=sys.stderr)
        return {"ok": True, "display_id": str(entry.get("display_id", "")), "error": ""}

    def get_sidebar_collapsed(self) -> list[str]:
        return sidebar_state.load()

    def save_sidebar_collapsed(self, collapsed: list) -> bool:
        if not isinstance(collapsed, list):
            return False
        return sidebar_state.save([str(x) for x in collapsed if isinstance(x, str)])

    def get_composer_height(self) -> int:
        return composer_state.load()

    def save_composer_height(self, height_px: int) -> bool:
        if not isinstance(height_px, (int, float)):
            return False
        return composer_state.save(int(height_px))

    def flash_window(self) -> bool:
        """Flash our taskbar button — called from the frontend when Claude
        finishes a turn or surfaces an interactive prompt. Internally skipped
        if the window is already foreground, so safe to call unconditionally.
        """
        return flash_taskbar(self._app._hwnd)

    def refresh_usage(self) -> bool:
        """Force an immediate /api/oauth/usage fetch + push (T-2026-196).

        Runs synchronously on the pywebview js-api worker thread; the frontend
        awaits the returned promise, so the refresh button can show a spinner
        for the exact duration of the HTTP call (capped by the 5s urlopen
        timeout inside usage_fetcher). ``on_update`` fires inline before we
        return, so by the time JS resumes, the new payload is already rendered.
        """
        poller = self._app._usage_poller
        if poller is None:
            return False
        try:
            poller.fetch_now()
        except Exception as e:
            print(f"[gui] refresh_usage failed: {e}", file=sys.stderr)
            return False
        return True

    # --- session / tab surface ---------------------------------------------------

    def picker_context(self) -> dict:
        """Initial context for the themed folder picker (recents, home, initial path)."""
        return picker.picker_context()

    def list_directory(self, path: Optional[str] = None) -> dict:
        """List subdirectories of `path` for the themed folder picker."""
        return picker.list_directory(path)

    def open_session(self, cwd: str) -> Optional[str]:
        """Spawn a new tab at ``cwd``; returns the new session_id or None."""
        if not isinstance(cwd, str) or not cwd:
            return None
        return self._app.open_session(cwd)

    def switch_session(self, session_id: str) -> bool:
        if not isinstance(session_id, str):
            return False
        return self._app.switch_to(session_id)

    def close_session(self, session_id: str) -> bool:
        if not isinstance(session_id, str):
            return False
        return self._app.close_session(session_id)

    def resolve_permission(
        self,
        pending_id: str,
        behavior: str,
        message: str = "",
        updated_input: Optional[dict] = None,
    ) -> bool:
        """Send a user decision back to a pending MCP permission request.

        `behavior` must be "allow" or "deny". `updated_input` is optional on
        allow and ignored on deny. Returns False if the pending_id is
        unknown (already resolved or timed out)."""
        if not isinstance(pending_id, str) or behavior not in ("allow", "deny"):
            return False
        return self._app.resolve_permission(
            pending_id,
            behavior,
            updated_input=updated_input if isinstance(updated_input, dict) else None,
            message=message if isinstance(message, str) else "",
        )


class App:
    def __init__(self) -> None:
        self.window = None
        self._hwnd: Optional[int] = None
        self._sessions: list[Session] = []
        # The active tab is identified by session_id, never by list index: a
        # concurrent close shifts every index after it, so an index held across
        # two statements can point at a different (or stopped) session by the
        # time it is dereferenced (review gui #5).
        self._active_id: str = ""
        # One lock for every mutation/read of _sessions, _active_id and the two
        # pending-permission dicts (review gui #5/#6). pywebview runs each
        # `pywebview.api.*` call on its own thread, the permission bridge's HTTP
        # handler runs on another, and the pty/tail threads push from a third.
        #
        # LOCK ORDER RULE: never hold _state_lock while calling _eval / any
        # _push_* helper. evaluate_js is a synchronous round-trip into the
        # webview, and blocking every other bridge thread behind it would make
        # a slow frontend stall the pty. Take the lock, snapshot what you need,
        # release, then push.
        self._state_lock = threading.RLock()
        self._theme_watcher: Optional[ThemeWatcher] = None
        self._usage_poller: Optional[usage_fetcher.UsagePoller] = None
        self._last_usage: Optional[dict] = None
        self._js_lock = threading.Lock()
        self._services_started = False
        # Drag-drop / paste file registries are per tab — each Session owns its
        # own FileRefs keyed by session_id (see file_refs.py), so staged files
        # belong to the tab they were added in. Wipe every scope now so this
        # run never inherits last run's references or pasted temp files.
        file_refs.FileRefs.wipe_all()
        # Pty capture is opt-in via APIARY_GUI_CAPTURE_LABEL. One CaptureWriter
        # for the lifetime of the process — all sessions write to the same file
        # prefixed by their label.
        self._capture: Optional[pty_capture.CaptureWriter] = None
        capture_env = os.environ.get("APIARY_GUI_CAPTURE_LABEL")
        if capture_env is not None:
            path = pty_capture.next_capture_path(capture_env or None)
            self._capture = pty_capture.CaptureWriter(path)
            print(f"[gui] pty capture → {path}", file=sys.stderr)
        # Permission-prompt MCP bridge (see scribe C-2026-36). Booted lazily
        # in start_services so the port env var is set before the first pty
        # spawn. None when APIARY_PERMISSION_MCP is off.
        self._permission_bridge: Optional[PermissionBridge] = None
        # Per-session pending permission prompt — (pending_id, payload) keyed
        # by session_id. Used to route a non-active tab's prompt to the tab
        # when the user switches to it, and to flag the tab chip as pending
        # via _sessions_descriptor. Follow-up #4 on scribe C-2026-36.
        self._pending_permission_by_session: dict[str, tuple[str, dict]] = {}
        # pending_id → session_id reverse lookup so resolve_permission can
        # clear the per-session entry without scanning.
        self._session_by_pending_id: dict[str, str] = {}

    @property
    def active(self) -> Optional[Session]:
        """The Session for the selected tab, resolved by id (never by index)."""
        with self._state_lock:
            return self._find(self._active_id)

    def _find(self, session_id: str) -> Optional[Session]:
        """Session with this id, or None. Caller holds ``_state_lock``."""
        if not session_id:
            return None
        for s in self._sessions:
            if s.session_id == session_id:
                return s
        return None

    # --- frontend bridge helpers --------------------------------------------------

    def _eval(self, js: str) -> None:
        win = self.window
        if win is None:
            return
        with self._js_lock:
            try:
                win.evaluate_js(js)
            except Exception as e:
                # Keep going — the window may have closed during shutdown.
                print(f"[gui] evaluate_js failed: {e}", file=sys.stderr)

    # Each push carries the originating session_id so the frontend can route
    # the event to the right tab. For backward compatibility (and the theme /
    # notes channels which are app-global), session_id defaults to "".

    def _push_message(self, msg: Message, session_id: str = "") -> None:
        payload = json.dumps(msg.to_dict())
        self._eval(f"window.apiary.onMessage({payload}, {json.dumps(session_id)});")

    def _push_messages(self, msgs: list[Message], session_id: str = "") -> None:
        if not msgs:
            return
        payload = json.dumps([m.to_dict() for m in msgs])
        self._eval(f"window.apiary.onMessages({payload}, {json.dumps(session_id)});")

    def _push_clear(self, session_id: str = "") -> None:
        self._eval(f"window.apiary.onClear({json.dumps(session_id)});")

    def _push_status(self, text: str, session_id: str = "") -> None:
        self._eval(f"window.apiary.onStatus({json.dumps(text)}, {json.dumps(session_id)});")

    def _push_toast(self, text: str, kind: str = "", session_id: str = "") -> None:
        self._eval(
            f"window.apiary.onToast({json.dumps(text)}, {json.dumps(kind)}, {json.dumps(session_id)});"
        )

    def _push_pty_chunk(self, chunk: str, session_id: str = "") -> None:
        self._eval(f"window.apiary.onPtyChunk({json.dumps(chunk)}, {json.dumps(session_id)});")

    def _push_pty_exit(self, code: int, session_id: str = "") -> None:
        self._eval(f"window.apiary.onPtyExit({json.dumps(code)}, {json.dumps(session_id)});")

    def _push_notes(
        self, notes: list[NoteEntry], warnings: list[str], session_id: str = ""
    ) -> None:
        payload = json.dumps([n.to_dict() for n in notes])
        self._eval(f"window.apiary.onNotes({payload}, {json.dumps(session_id)});")
        for w in warnings:
            print(f"[scribe] {w}", file=sys.stderr)

    def _push_agents(self, agents: list, session_id: str = "") -> None:
        payload = json.dumps([a.to_dict() for a in agents])
        self._eval(f"window.apiary.onAgents({payload}, {json.dumps(session_id)});")

    def _push_theme(self, vars_: dict, err: Optional[str]) -> None:
        if err:
            self._push_toast(err, "error")
        self._eval(f"window.apiary.onTheme({json.dumps(vars_)});")

    def _push_permission_prompt(self, pending_id: str, payload: dict) -> None:
        """Surface a permission request from the MCP bridge to the frontend.
        Called from the bridge's HTTP handler thread; returns immediately —
        the decision arrives later via GuiBridge.resolve_permission.

        The prompt is stored keyed by ``session_id`` (from the payload) so
        the GUI can badge a non-active tab and surface the banner when the
        user switches to that tab. If session_id is missing (older MCP
        subprocess, test harness, etc.) the prompt is still pushed to the
        webview but without per-tab routing.
        """
        session_id = payload.get("session_id") or ""
        if session_id:
            with self._state_lock:
                self._pending_permission_by_session[session_id] = (pending_id, payload)
                self._session_by_pending_id[pending_id] = session_id
            # Refresh the tabs descriptor so the non-active tab gets its
            # pending-permission badge. (No-op on the banner itself.)
            self._push_sessions()
        self._eval(
            f"window.apiary.onPermissionPrompt({json.dumps(pending_id)}, {json.dumps(payload)});"
        )

    def _push_ask_prompt(self, payload: dict, session_id: str = "") -> None:
        """Surface a pending AskUserQuestion to the frontend banner.

        Sourced from the transcript tool_use record (gui.ask_prompt), NOT the
        xterm scrape — so the option list is complete and exact regardless of
        the pty viewport height. The JS side routes by session_id and ignores
        prompts for non-active tabs.
        """
        self._eval(f"window.apiary.onAskPrompt({json.dumps(payload)}, {json.dumps(session_id)});")

    def _push_ask_prompt_resolved(self, tool_use_id: str, session_id: str = "") -> None:
        """Tell the frontend a pending AskUserQuestion was answered/cancelled so
        it can clear the banner (the matching tool_result arrived)."""
        self._eval(
            "window.apiary.onAskPromptResolved("
            f"{json.dumps(tool_use_id)}, {json.dumps(session_id)}"
            ");"
        )

    def _start_permission_bridge(self) -> None:
        if not permission_mcp.mcp_enabled(load_launch()):
            return
        if self._permission_bridge is not None:
            return
        bridge = PermissionBridge(on_request=self._push_permission_prompt)
        try:
            url = bridge.start()
        except OSError as e:
            # Fail closed. With no bridge the MCP server denies every call,
            # so pin the flag OFF: session.py then spawns claude without
            # --permission-prompt-tool and prompts come through the TUI
            # banner path instead of a dead gate (review C-2).
            os.environ["APIARY_PERMISSION_MCP"] = "0"
            os.environ.pop(_PERMISSION_MCP_BRIDGE_URL_ENV, None)
            print(
                f"[gui] permission bridge failed to boot: {e}; "
                "MCP permission path disabled for this run",
                file=sys.stderr,
            )
            return
        self._permission_bridge = bridge
        # Exported via process env so the claude subprocess (and, downstream,
        # the MCP stdio server spawned by claude) can POST decisions here.
        os.environ[_PERMISSION_MCP_BRIDGE_URL_ENV] = url
        # Only now pin the flag to "1" so session.py (and anything else that
        # reads it downstream) sees a consistent source of truth regardless
        # of whether it came from env or launch.json — and never sees "1"
        # without a live bridge behind it.
        os.environ["APIARY_PERMISSION_MCP"] = "1"
        print(f"[gui] permission bridge listening at {url}", file=sys.stderr)

    def resolve_permission(
        self,
        pending_id: str,
        behavior: str,
        *,
        updated_input: Optional[dict] = None,
        message: str = "",
    ) -> bool:
        if self._permission_bridge is None:
            return False
        decision: dict = {"behavior": behavior}
        if behavior == "allow":
            decision["updatedInput"] = updated_input or {}
        elif message:
            decision["message"] = message
        ok = self._permission_bridge.resolve(pending_id, decision)
        # Clear per-tab pending regardless of resolve outcome — a stale
        # pending that can't be delivered (bridge closed, timed out) should
        # not keep the tab chip badged forever.
        with self._state_lock:
            session_id = self._session_by_pending_id.pop(pending_id, "")
            cleared = False
            stored = self._pending_permission_by_session.get(session_id)
            if stored is not None and stored[0] == pending_id:
                self._pending_permission_by_session.pop(session_id, None)
                cleared = True
        if cleared:
            self._push_sessions()
        return ok

    def _push_usage(self, payload: Optional[dict]) -> None:
        """Push the /api/oauth/usage response (or None on failure) to the
        frontend. ``null`` tells the UI to show a "—" / stale indicator.

        Caches the last successful payload so page reloads (Ctrl+R) can
        re-render immediately instead of waiting for the next 60s tick.
        """
        if payload is not None:
            self._last_usage = payload
        self._eval(f"window.apiary.onUsage({json.dumps(payload)});")

    # --- drag-dropped file references ---------------------------------------------

    def _register_file_drop(self) -> None:
        """Register the native pywebview drop handler on the document.

        Subscribed to ``window.events.loaded`` so it re-arms after a page reload
        (the DOM element registry and the JS listeners are torn down on reload).
        Only the ``drop`` event is registered in Python — dragover is cancelled
        in JS (file_drop.js) to avoid spawning a Python thread per dragover tick.
        """
        win = self.window
        if win is None:
            return
        try:
            from webview.dom import DOMEventHandler  # type: ignore

            # prevent_default stops the webview from navigating to the dropped
            # file. pywebview stamps each file with pywebviewFullPath before our
            # handler runs (webview/util.py), giving us the real path for free.
            win.dom.document.on("drop", DOMEventHandler(self._on_file_drop, prevent_default=True))
        except Exception as e:
            print(f"[gui] file-drop registration failed: {e}", file=sys.stderr)

    def _on_file_drop(self, event: dict) -> None:
        """Handle a native file drop: record a reference per dropped file, then
        push the updated list to the frontend. Runs on a pywebview worker
        thread; ``_eval`` is thread-safe."""
        try:
            files = (event or {}).get("dataTransfer", {}).get("files", []) or []
        except AttributeError:
            return
        sess = self.active
        if sess is None:
            return  # a drop with no open tab has nowhere to land
        added = False
        for f in files:
            if not isinstance(f, dict):
                continue
            path = f.get("pywebviewFullPath")
            if not path:
                continue
            if sess.file_refs.add(path).get("ok"):
                added = True
        if added:
            self._push_file_refs()

    def _push_file_refs(self) -> None:
        """Push the ACTIVE tab's file list to the (single, shared) refs panel.
        Called on drop and on every tab switch so the panel always reflects the
        active tab — switching tabs swaps the staged files in view."""
        sess = self.active
        payload = json.dumps(sess.file_refs.list() if sess is not None else [])
        self._eval(f"window.apiaryFileDrop && window.apiaryFileDrop.setFiles({payload});")

    # --- session lifecycle --------------------------------------------------------

    def _create_session(
        self,
        cwd: Path,
        accept_edits: bool = False,
    ) -> Session:
        """Build a Session wired to push UI events through App callbacks."""
        launch = load_launch()
        return Session(
            cwd=cwd,
            on_message=self._push_message,
            on_messages=self._push_messages,
            on_clear=self._push_clear,
            on_status=self._push_status,
            on_pty_chunk=self._push_pty_chunk,
            on_pty_exit=self._push_pty_exit,
            on_toast=self._push_toast,
            on_notes=self._push_notes,
            on_agents=self._push_agents,
            on_ask_prompt=self._push_ask_prompt,
            on_ask_prompt_resolved=self._push_ask_prompt_resolved,
            capture=self._capture,
            command=launch.get("command", "claude"),
            args=list(launch.get("args", [])),
            rows=int(launch.get("rows", 40) or 40),
            cols=int(launch.get("cols", 120) or 120),
            accept_edits=accept_edits,
        )

    def _sessions_descriptor(self) -> list[dict]:
        """Frontend-facing view of open tabs. Caller holds ``_state_lock``."""
        return [
            {
                "session_id": s.session_id,
                "cwd": str(s.cwd),
                "label": s.cwd.name or str(s.cwd),
                "active": (s.session_id == self._active_id),
                "accept_edits": bool(s.accept_edits),
                "pending_permission": s.session_id in self._pending_permission_by_session,
            }
            for s in self._sessions
        ]

    def _sweep_stale_pending_permissions(self) -> None:
        """Drop per-tab pending entries whose pending_id is no longer tracked
        by the bridge — either because it resolved internally (5-min timeout
        deny) or the MCP subprocess crashed before the bridge registered it.

        Called reactively from ``_push_sessions`` so badges clear the next
        time any tab state is refreshed. Cheap: the bridge's pending set is
        bounded by the number of concurrent permission requests in flight.

        Caller holds ``_state_lock``.
        """
        if self._permission_bridge is None:
            return
        live = set(self._permission_bridge.pending_ids())
        for sid in list(self._pending_permission_by_session.keys()):
            pending_id, _ = self._pending_permission_by_session[sid]
            if pending_id not in live:
                self._pending_permission_by_session.pop(sid, None)
                self._session_by_pending_id.pop(pending_id, None)

    def _push_sessions(self) -> None:
        with self._state_lock:
            self._sweep_stale_pending_permissions()
            payload = json.dumps(self._sessions_descriptor())
        self._eval(f"window.apiary.onSessions({payload});")

    def _push_active_session(self) -> None:
        sess = self.active
        sid = json.dumps(sess.session_id if sess else "")
        self._eval(f"window.apiary.setActiveSession({sid});")
        # Swap the refs panel to the now-active tab's staged files (each tab has
        # its own registry). Centralized here so open/switch/close/restore all
        # keep the panel in sync without each repeating the push.
        self._push_file_refs()

    def _persist_tabs(self) -> None:
        with self._state_lock:
            entries = [TabEntry(cwd=s.cwd, accept_edits=s.accept_edits) for s in self._sessions]
            # tabs.json is index-based (it predates ids and survives a restart,
            # where ids do not); derive the index from the active id at write
            # time so the id stays the single in-memory source of truth.
            active_idx = -1
            for i, s in enumerate(self._sessions):
                if s.session_id == self._active_id:
                    active_idx = i
                    break
        tabs_state.save(entries, active_idx)

    def open_session(self, cwd: str) -> Optional[str]:
        """Create a new Session at ``cwd`` and make it active. Returns the new
        session_id on success, None on spawn failure. Called by the bridge
        when the user picks a directory.
        """
        try:
            path = Path(cwd).resolve()
        except Exception as e:
            self._push_toast(f"Invalid directory: {e}", "error")
            return None
        if not path.is_dir():
            self._push_toast(f"Not a directory: {path}", "error")
            return None
        sess = self._create_session(path)
        if not sess.start():
            return None
        with self._state_lock:
            self._sessions.append(sess)
            self._active_id = sess.session_id
        self._push_active_session()
        self._push_sessions()
        sess.flush_notes()
        self._persist_tabs()
        return sess.session_id

    def switch_to(self, session_id: str) -> bool:
        """Make ``session_id`` the active tab. Frontend pushes then apply to it."""
        with self._state_lock:
            if self._find(session_id) is None:
                return False
            if session_id == self._active_id:
                return True
            self._active_id = session_id
        self._push_active_session()
        self._push_sessions()
        self._replay_active()
        self._persist_tabs()
        return True

    def close_session(self, session_id: str) -> bool:
        """Stop and remove a session. If it was active, promote the next tab.

        Returns False when the id is unknown — including the second of two
        racing closes of the same tab, which is what makes a double-click on
        the ``×`` safe (review gui #5).
        """
        with self._state_lock:
            sess = self._find(session_id)
            if sess is None:
                return False
            idx = self._sessions.index(sess)
            self._sessions.pop(idx)
            pending = self._pending_permission_by_session.pop(session_id, None)
            if pending is not None:
                self._session_by_pending_id.pop(pending[0], None)
            if session_id == self._active_id:
                # Promote the tab that slid into the closed one's slot, or the
                # new last tab when the closed one was rightmost.
                if self._sessions:
                    self._active_id = self._sessions[min(idx, len(self._sessions) - 1)].session_id
                else:
                    self._active_id = ""
            # Closing a NON-active tab leaves _active_id alone — that is the
            # whole point of keying on id: no index to decrement, nothing to
            # get wrong when two closes interleave.

        # Everything below is slow or re-entrant (pty teardown, disk, JS) and
        # runs outside the lock — see the LOCK ORDER RULE in __init__.
        if pending is not None and self._permission_bridge is not None:
            # Deny the pending prompt so the MCP subprocess unblocks now
            # instead of waiting out the 5-min bridge timeout.
            try:
                self._permission_bridge.resolve(
                    pending[0], {"behavior": "deny", "message": "tab closed"}
                )
            except Exception:
                pass
        try:
            sess.stop()
        except Exception:
            pass
        # Tear down this tab's file registry: delete its owned (pasted)
        # temp files, store, and pasted subdir so a closed tab leaves
        # nothing behind. Dropped references' targets are untouched.
        try:
            sess.file_refs.destroy()
        except Exception:
            pass
        self._push_active_session()
        self._push_sessions()
        self._replay_active()
        self._persist_tabs()
        return True

    def _replay_active(self) -> None:
        """Re-render whatever tab is active now, from the backend's own state.

        The one replay path for every caller that changes what the chat pane
        is showing — tab switch, tab close, and page reload each used to
        hand-roll their own copy and had drifted apart, so a reload lost the
        agents strip on a switch and a pending permission banner on either
        (review gui: "Transcript replay block appears three times").

        Pushes, in order: clear → transcript history → scribe notes → agent
        snapshot → any pending permission prompt. Every step is independently
        guarded: a tab whose transcript has been deleted still gets its notes.

        NOTE (review gui #10): this re-reads the whole JSONL and ships it as
        one evaluate_js string. Cheap for normal transcripts, multi-MB for
        long ones. Chunking/virtualising the render is a separate change.
        """
        sess = self.active
        if sess is None:
            return
        sid = sess.session_id
        self._push_clear(sid)
        path = sess.current_path
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self._push_status(f"Cannot re-read transcript: {e}", sid)
            else:
                self._push_messages(parse_jsonl_lines(text), sid)
        # Notes immediately so the sidebar doesn't flash empty while waiting
        # on the next 5s aggregator tick.
        try:
            sess.flush_notes()
        except Exception as e:
            print(f"[gui] notes resync failed: {e}", file=sys.stderr)
        # The tracker dedups by payload key and won't re-emit on its own, so
        # without this the agents strip stays empty until an agent changes.
        if sess.subagent_tracker is not None:
            try:
                self._push_agents(sess.subagent_tracker.snapshot(), sid)
            except Exception as e:
                print(f"[gui] agents resync failed: {e}", file=sys.stderr)
        # A prompt raised while this tab was in the background (or before a
        # reload) is still blocking an MCP call — surface it now that the tab
        # is on screen. The JS side dropped any banner from the previous tab.
        with self._state_lock:
            pending = self._pending_permission_by_session.get(sid)
        if pending is not None:
            pending_id, payload = pending
            self._eval(
                "window.apiary.onPermissionPrompt("
                f"{json.dumps(pending_id)}, {json.dumps(payload)}"
                ");"
            )

    def start_services(self) -> None:
        # Webview 'loaded' fires again on every page reload (Ctrl+F5). Without
        # the idempotence guard, every reload would leak a fresh ThemeWatcher,
        # aggregator, and session (whose pty would double-spawn claude and
        # whose un-pinned discovery could latch onto the OUTER Claude Code
        # session the dev launched from). Instead, just re-push state.
        if self._services_started:
            self._resync_frontend_state()
            return
        self._services_started = True

        # Boot the permission bridge before any session spawn so the URL env
        # is set when session.py copies os.environ into the pty subprocess.
        self._start_permission_bridge()

        ensure_theme_defaults()
        vars_, theme_err = load_theme()
        self._push_theme(vars_, theme_err)
        self._theme_watcher = ThemeWatcher(on_reload=self._push_theme)
        self._theme_watcher.start()

        # Restore saved tabs from the last run. When there's no saved state,
        # prefer launch.json's explicit cwd (useful for devs and repeat users
        # who configured the GUI). Otherwise show the empty-state picker so
        # first-time users aren't dropped into an arbitrary cwd like C:\.
        entries, active_idx = tabs_state.load()
        if not entries:
            launch = load_launch()
            launch_cwd = launch.get("cwd") or ""
            if launch_cwd:
                entries = [TabEntry(cwd=Path(launch_cwd))]
                active_idx = 0

        started: list[Session] = []
        for entry in entries:
            sess = self._create_session(
                entry.cwd,
                accept_edits=entry.accept_edits,
            )
            if sess.start():
                started.append(sess)
        if started:
            with self._state_lock:
                self._sessions.extend(started)
                # tabs.json stores an index; translate it to an id once, here.
                self._active_id = self._sessions[
                    max(0, min(active_idx, len(self._sessions) - 1))
                ].session_id
            self._push_active_session()
            self._push_sessions()
            # Flush notes once for the active tab (backend aggregators only
            # emit on their 5s tick, so this bridges the startup gap).
            active = self.active
            if active is not None:
                active.flush_notes()
            self._persist_tabs()
        else:
            # No sessions: push empty state so frontend shows the picker CTA.
            self._push_active_session()
            self._push_sessions()

        # Live quota meter poller (T-2026-25). 60s interval — the 5-hour
        # bucket barely moves faster than that, and the endpoint is flaky
        # enough (GH issue #31021 / L-2026-114) that we don't want to hammer
        # it. One-shot fetch first so the sidebar populates before the first
        # timer tick.
        self._usage_poller = usage_fetcher.UsagePoller(on_update=self._push_usage, interval=60.0)
        threading.Thread(target=self._usage_poller.fetch_now, daemon=True).start()
        self._usage_poller.start()

    def _resync_frontend_state(self) -> None:
        """Re-push current state to the webview after a page reload."""
        vars_, theme_err = load_theme()
        self._push_theme(vars_, theme_err)
        if self.active is not None:
            self._push_active_session()
            self._push_sessions()
            self._replay_active()
        # Re-push the cached usage payload immediately so meters don't flash
        # empty while waiting for the next poll tick.
        if self._last_usage is not None:
            self._push_usage(self._last_usage)

    def shutdown(self) -> None:
        with self._state_lock:
            sessions = self._sessions
            self._sessions = []
            self._active_id = ""
            self._pending_permission_by_session.clear()
            self._session_by_pending_id.clear()
        for sess in sessions:
            try:
                sess.stop()
            except Exception:
                pass
        if self._theme_watcher is not None:
            self._theme_watcher.stop()
        if self._usage_poller is not None:
            try:
                self._usage_poller.stop()
            except Exception:
                pass
            self._usage_poller = None
        if self._capture is not None:
            self._capture.close()
        if self._permission_bridge is not None:
            try:
                self._permission_bridge.stop()
            except Exception:
                pass
            self._permission_bridge = None
            os.environ.pop(_PERMISSION_MCP_BRIDGE_URL_ENV, None)


def _webview2_installed() -> bool:
    """True if the Edge WebView2 Evergreen runtime is registered (Windows).

    pywebview renders through WebView2; a missing runtime is a common cause of
    "the window won't open" on a fresh machine. Mirrors scripts/preflight.py,
    kept independent so neither has to import the other.
    """
    if os.name != "nt":
        return True  # not applicable; don't warn on non-Windows
    try:
        import winreg
    except ImportError:
        return True
    client = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + client,
        ),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + client),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\\" + client),
    ]
    for hive, subkey in locations:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _environment_warnings() -> list[str]:
    """Non-fatal startup checks for the two things most likely to make the
    window fail to render or tabs fail to spawn. Returned with remediation so a
    terminal launch shows the user exactly what to fix."""
    warnings: list[str] = []
    if not _webview2_installed():
        warnings.append(
            "Edge WebView2 runtime not detected — the window may fail to render. "
            "Install it: https://developer.microsoft.com/microsoft-edge/webview2/"
        )
    try:
        from gui.pty_wrapper import _resolve_claude_command

        if _resolve_claude_command("claude") is None:
            warnings.append(
                "the `claude` CLI was not found on PATH — sessions can't start "
                "until Claude Code is installed (https://claude.ai/claude-code)."
            )
    except Exception:
        pass
    return warnings


def main() -> int:
    # Frozen-bundle MCP dispatch: claude-code spawns the MCP server via the
    # config written by `write_mcp_config()`, which in a PyInstaller build
    # points at this same exe with `--mcp-server`. Route straight into the
    # stdio server and skip the webview + SingleInstance mutex so multiple
    # MCP subprocesses can coexist with the main GUI.
    if len(sys.argv) >= 2 and sys.argv[1] == permission_mcp.MCP_SERVER_FLAG:
        permission_mcp.serve()
        return 0

    # WebView2 aggressively caches app.js / app.css / vendored assets across
    # launches, which makes iterating on the frontend painful — edits don't
    # appear until a manual Ctrl+F5. Disable disk cache for our WebView2
    # browser. Must be set BEFORE webview is imported (WebView2 reads the env
    # var at browser-process start).
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
        + " --disable-cache --disable-application-cache --disk-cache-size=1"
    ).strip()

    # APIARY_GUI_PROFILE re-roots state, mutex, and window title — see gui/paths.py.
    from gui.paths import mutex_name, profile, window_title

    with SingleInstance(name=mutex_name()) as guard:
        if not guard.acquired:
            label = f" ({profile()})" if profile() else ""
            print(f"apiary GUI is already running{label}.", file=sys.stderr)
            return 0

        try:
            import webview  # type: ignore
        except ImportError:
            print(
                "pywebview is not installed. Run `poetry install --with gui`",
                file=sys.stderr,
            )
            return 1

        if not INDEX_HTML.is_file():
            print(f"missing frontend bundle at {INDEX_HTML}", file=sys.stderr)
            return 1

        # Which build is this? Frozen bundles read the commit stamped in at
        # build time; from source it's resolved live from git. Printed before
        # anything can go wrong, so a bug report can name a tree state.
        print(f"apiary GUI {build_info.version_string()}", file=sys.stderr)

        for _w in _environment_warnings():
            print(f"warning: {_w}", file=sys.stderr)

        app = App()
        bridge = GuiBridge(app)

        title = window_title()
        # Boot-time title used only for HWND lookup — window_title() may be empty
        # (unbranded), which FindWindowW can't match. Reset to the real title
        # after the DWM dark-mode attribute is applied; the attribute persists.
        boot_title = f"apiary-gui-boot-{os.getpid()}"
        window = webview.create_window(
            title=boot_title,
            url=str(INDEX_HTML),
            js_api=bridge,
            width=1400,
            height=900,
            min_size=(560, 480),
            background_color="#0e1116",
        )
        app.window = window

        def _on_loaded() -> None:
            # Apply Win11 dark titlebar after window has rendered (HWND now exists).
            # We look up by title rather than relying on pywebview internals.
            hwnd = find_window_by_title(boot_title)
            app._hwnd = hwnd
            apply_dark_titlebar(hwnd)
            window.set_title(title)
            app.start_services()

        def _on_closed() -> None:
            app.shutdown()

        window.events.loaded += _on_loaded
        # Separate subscriber so the drop handler re-arms on every page (re)load,
        # not just first boot — start_services is one-shot and wouldn't re-run.
        window.events.loaded += app._register_file_drop
        window.events.closed += _on_closed

        webview.start(debug=bool(os.environ.get("APIARY_GUI_DEBUG")))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
