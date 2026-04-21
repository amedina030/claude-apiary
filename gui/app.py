"""apiary GUI entry point — `python -m gui.app`.

Spawns a PyWebView window and owns a list of Sessions — one per open tab.
Each Session bundles its own claude pty subprocess, transcript tail, and
session-discovery; App routes bridge calls and UI pushes to the active one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from gui import composer_state, pty_capture, sidebar_state, tabs_state, usage_fetcher
from gui.scribe_aggregator import NoteEntry, read_body
from gui.session import Session
from gui.single_instance import SingleInstance
from gui.tabs_state import TabEntry
from gui.theme import (
    ThemeWatcher,
    ensure_defaults as ensure_theme_defaults,
    load_launch,
    load_theme,
)
from gui.transcript import Message, parse_jsonl_lines
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


class GuiBridge:
    """JS → Python surface (frontend calls `pywebview.api.<method>()`).

    Every pty-touching method routes to ``app.active`` — the Session for the
    currently-selected tab. Returns False when there is no active session.
    """

    def __init__(self, app: "App") -> None:
        self._app = app

    def ping(self) -> str:
        return "ok"

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

    def restart_pty(self) -> bool:
        sess = self._app.active
        return sess.restart_pty() if sess is not None else False

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

    def get_note_body(self, body_path: str) -> str:
        """Frontend calls this when a sidebar note is clicked. Read-only — no scribe writes."""
        if not isinstance(body_path, str):
            return ""
        return read_body(body_path)

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

    # --- session / tab surface ---------------------------------------------------

    def list_sessions(self) -> list[dict]:
        return self._app._sessions_descriptor()

    def pick_directory(self) -> Optional[str]:
        """Open a native folder picker; returns the chosen path or None."""
        return self._app.pick_directory()

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

    def set_session_setting(self, session_id: str, key: str, value) -> bool:
        """Per-tab permission toggles (T-2026-176).

        key='accept_edits'  -> restarts the pty so --permission-mode acceptEdits
                               takes effect on the next claude invocation.
        key='allow_self_edits' -> frontend-only state; no restart needed.
        """
        if not isinstance(session_id, str) or not isinstance(key, str):
            return False
        return self._app.set_session_setting(session_id, key, bool(value))


class App:
    def __init__(self) -> None:
        self.window = None
        self._sessions: list[Session] = []
        self._active_idx: int = -1
        self._theme_watcher: Optional[ThemeWatcher] = None
        self._usage_poller: Optional[usage_fetcher.UsagePoller] = None
        self._last_usage: Optional[dict] = None
        self._js_lock = threading.Lock()
        self._services_started = False
        # Pty capture is opt-in via APIARY_GUI_CAPTURE_LABEL. One CaptureWriter
        # for the lifetime of the process — all sessions write to the same file
        # prefixed by their label.
        self._capture: Optional[pty_capture.CaptureWriter] = None
        capture_env = os.environ.get("APIARY_GUI_CAPTURE_LABEL")
        if capture_env is not None:
            path = pty_capture.next_capture_path(capture_env or None)
            self._capture = pty_capture.CaptureWriter(path)
            print(f"[gui] pty capture → {path}", file=sys.stderr)

    @property
    def active(self) -> Optional[Session]:
        if 0 <= self._active_idx < len(self._sessions):
            return self._sessions[self._active_idx]
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
        self._eval(
            f"window.apiary.onPtyChunk({json.dumps(chunk)}, {json.dumps(session_id)});"
        )

    def _push_pty_exit(self, code: int, session_id: str = "") -> None:
        self._eval(
            f"window.apiary.onPtyExit({json.dumps(code)}, {json.dumps(session_id)});"
        )

    def _push_notes(self, notes: list[NoteEntry], warnings: list[str], session_id: str = "") -> None:
        payload = json.dumps([n.to_dict() for n in notes])
        self._eval(
            f"window.apiary.onNotes({payload}, {json.dumps(session_id)});"
        )
        for w in warnings:
            print(f"[scribe] {w}", file=sys.stderr)

    def _push_theme(self, vars_: dict, err: Optional[str]) -> None:
        if err:
            self._push_toast(err, "error")
        self._eval(f"window.apiary.onTheme({json.dumps(vars_)});")

    def _push_handoff_banner(self, count: int) -> None:
        self._eval(f"window.apiary.onHandoffBanner({json.dumps(int(count))});")

    def _push_usage(self, payload: Optional[dict]) -> None:
        """Push the /api/oauth/usage response (or None on failure) to the
        frontend. ``null`` tells the UI to show a "—" / stale indicator.

        Caches the last successful payload so page reloads (Ctrl+R) can
        re-render immediately instead of waiting for the next 60s tick.
        """
        if payload is not None:
            self._last_usage = payload
        self._eval(f"window.apiary.onUsage({json.dumps(payload)});")

    def _check_unfilled_handoffs(self) -> int:
        """Count handoff-less sessions via core/startup.py unseen. Returns 0 on
        any failure (fail-open — banner is a nudge, not critical).

        The launcher's CLAUDE_CODE_SESSION_ID (present when the GUI was spawned
        from inside a claude-code session) is forwarded as --session-id so that
        session isn't counted against itself. The GUI's own spawned subprocess
        sessions haven't registered yet at this point, so they're naturally
        absent from the unseen list.

        Repo resolution is left to ``apiary_launch.py`` (reads
        ``~/.claude/apiary.json``), so this works from the packaged exe and
        from dev-mode ``python -m gui.app`` alike.
        """
        launcher = Path.home() / ".claude" / "apiary_launch.py"
        if not launcher.is_file():
            return 0
        excluded_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        cmd = [
            sys.executable,
            str(launcher),
            "core/startup.py",
            "unseen",
            "--session-id",
            excluded_sid,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[gui] handoff check failed: {e}", file=sys.stderr)
            return 0
        if result.returncode != 0:
            print(
                f"[gui] handoff check exit={result.returncode} stderr={result.stderr!r}",
                file=sys.stderr,
            )
            return 0
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return 0
        return int(data.get("count", 0) or 0)

    # --- session lifecycle --------------------------------------------------------

    def _create_session(
        self,
        cwd: Path,
        accept_edits: bool = False,
        allow_self_edits: bool = False,
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
            capture=self._capture,
            command=launch.get("command", "claude"),
            args=list(launch.get("args", [])),
            rows=int(launch.get("rows", 40) or 40),
            cols=int(launch.get("cols", 120) or 120),
            accept_edits=accept_edits,
            allow_self_edits=allow_self_edits,
        )

    def _sessions_descriptor(self) -> list[dict]:
        """Frontend-facing view of open tabs."""
        return [
            {
                "session_id": s.session_id,
                "cwd": str(s.cwd),
                "label": s.cwd.name or str(s.cwd),
                "active": (i == self._active_idx),
                "accept_edits": bool(s.accept_edits),
                "allow_self_edits": bool(s.allow_self_edits),
            }
            for i, s in enumerate(self._sessions)
        ]

    def _push_sessions(self) -> None:
        payload = json.dumps(self._sessions_descriptor())
        self._eval(f"window.apiary.onSessions({payload});")

    def _push_active_session(self) -> None:
        sess = self.active
        sid = json.dumps(sess.session_id if sess else "")
        self._eval(f"window.apiary.setActiveSession({sid});")

    def _persist_tabs(self) -> None:
        tabs_state.save(
            [
                TabEntry(
                    cwd=s.cwd,
                    accept_edits=s.accept_edits,
                    allow_self_edits=s.allow_self_edits,
                )
                for s in self._sessions
            ],
            self._active_idx,
        )

    def set_session_setting(self, session_id: str, key: str, value: bool) -> bool:
        """Update a per-tab permission toggle.

        Mid-session behavior:
        - ``accept_edits``: stored only. Applied to the pty lazily — the
          frontend sends Shift+Tab chord(s) to cycle claude's live permission
          mode (no pty restart, session history preserved). The stored value
          is ALSO consumed on the NEXT spawn of this tab (new-tab creation or
          explicit restart) via --permission-mode acceptEdits.
        - ``allow_self_edits``: stored only; purely frontend — the prompt
          detector reads it to auto-ack .claude/ protect-self prompts.
        """
        for s in self._sessions:
            if s.session_id != session_id:
                continue
            if key == "accept_edits":
                if s.accept_edits == value:
                    return True
                s.accept_edits = value
            elif key == "allow_self_edits":
                s.allow_self_edits = value
            else:
                return False
            self._push_sessions()
            self._persist_tabs()
            return True
        return False

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
        self._sessions.append(sess)
        self._active_idx = len(self._sessions) - 1
        self._push_active_session()
        self._push_sessions()
        sess.flush_notes()
        self._persist_tabs()
        return sess.session_id

    def switch_to(self, session_id: str) -> bool:
        """Make ``session_id`` the active tab. Frontend pushes then apply to it."""
        for i, s in enumerate(self._sessions):
            if s.session_id == session_id:
                if i == self._active_idx:
                    return True
                self._active_idx = i
                self._push_active_session()
                self._push_sessions()
                # Re-push active session's transcript + clear so the UI rebuilds.
                self._push_clear(s.session_id)
                if s.current_path is not None:
                    try:
                        text = s.current_path.read_text(encoding="utf-8", errors="replace")
                        self._push_messages(parse_jsonl_lines(text), s.session_id)
                    except OSError as e:
                        self._push_status(f"Cannot re-read transcript: {e}", s.session_id)
                # Push this tab's scribe notes immediately so sidebar doesn't
                # flash empty while waiting on the next aggregator tick.
                s.flush_notes()
                self._persist_tabs()
                return True
        return False

    def close_session(self, session_id: str) -> bool:
        """Stop and remove a session. If it was active, promote the next tab."""
        for i, s in enumerate(self._sessions):
            if s.session_id == session_id:
                try:
                    s.stop()
                except Exception:
                    pass
                self._sessions.pop(i)
                if not self._sessions:
                    self._active_idx = -1
                elif self._active_idx >= len(self._sessions):
                    self._active_idx = len(self._sessions) - 1
                elif self._active_idx > i:
                    self._active_idx -= 1
                self._push_active_session()
                self._push_sessions()
                # If a new tab is now active, re-render its transcript.
                new_active = self.active
                if new_active is not None:
                    self._push_clear(new_active.session_id)
                    if new_active.current_path is not None:
                        try:
                            text = new_active.current_path.read_text(
                                encoding="utf-8", errors="replace"
                            )
                            self._push_messages(
                                parse_jsonl_lines(text), new_active.session_id
                            )
                        except OSError:
                            pass
                    new_active.flush_notes()
                self._persist_tabs()
                return True
        return False

    def pick_directory(self) -> Optional[str]:
        """Open a native folder picker and return the selected path, or None."""
        win = self.window
        if win is None:
            return None
        try:
            import webview  # type: ignore

            result = win.create_file_dialog(
                webview.FOLDER_DIALOG,
                allow_multiple=False,
            )
        except Exception as e:
            self._push_toast(f"Folder dialog failed: {e}", "error")
            return None
        if not result:
            return None
        # create_file_dialog returns a tuple/list of paths (or a single string
        # depending on pywebview version); take the first.
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)

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

        for entry in entries:
            sess = self._create_session(
                entry.cwd,
                accept_edits=entry.accept_edits,
                allow_self_edits=entry.allow_self_edits,
            )
            if sess.start():
                self._sessions.append(sess)
        if self._sessions:
            self._active_idx = max(0, min(active_idx, len(self._sessions) - 1))
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

        # Unfilled-handoff nudge (T-2026-164). Runs on a worker thread so the
        # 5s subprocess timeout can't delay the first webview paint.
        threading.Thread(
            target=lambda: self._push_handoff_banner(self._check_unfilled_handoffs()),
            daemon=True,
        ).start()

        # Live quota meter poller (T-2026-25). 60s interval — the 5-hour
        # bucket barely moves faster than that, and the endpoint is flaky
        # enough (GH issue #31021 / L-2026-114) that we don't want to hammer
        # it. One-shot fetch first so the sidebar populates before the first
        # timer tick.
        self._usage_poller = usage_fetcher.UsagePoller(
            on_update=self._push_usage, interval=60.0
        )
        threading.Thread(
            target=self._usage_poller.fetch_now, daemon=True
        ).start()
        self._usage_poller.start()

    def _resync_frontend_state(self) -> None:
        """Re-push current state to the webview after a page reload."""
        vars_, theme_err = load_theme()
        self._push_theme(vars_, theme_err)
        # Replay the active session's transcript so the chat shows history again.
        sess = self.active
        if sess is not None:
            self._push_active_session()
            self._push_sessions()
            if sess.current_path is not None:
                try:
                    text = sess.current_path.read_text(encoding="utf-8", errors="replace")
                    history = parse_jsonl_lines(text)
                    self._push_messages(history, sess.session_id)
                except OSError as e:
                    self._push_status(f"Cannot re-read transcript: {e}", sess.session_id)
            # One-shot notes refresh so the sidebar populates before the next
            # 5s aggregator tick.
            try:
                sess.flush_notes()
            except Exception as e:
                print(f"[gui] notes resync failed: {e}", file=sys.stderr)
        # Re-check unfilled handoffs on reload — user may have just run
        # /backfill-handoffs and the banner should reflect the new count.
        threading.Thread(
            target=lambda: self._push_handoff_banner(self._check_unfilled_handoffs()),
            daemon=True,
        ).start()
        # Re-push the cached usage payload immediately so meters don't flash
        # empty while waiting for the next poll tick.
        if self._last_usage is not None:
            self._push_usage(self._last_usage)

    def shutdown(self) -> None:
        for sess in self._sessions:
            try:
                sess.stop()
            except Exception:
                pass
        self._sessions = []
        self._active_idx = -1
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


def main() -> int:
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

        app = App()
        bridge = GuiBridge(app)

        title = window_title()
        window = webview.create_window(
            title=title,
            url=str(INDEX_HTML),
            js_api=bridge,
            width=1400,
            height=900,
            min_size=(800, 600),
            background_color="#0e1116",
        )
        app.window = window

        def _on_loaded() -> None:
            # Apply Win11 dark titlebar after window has rendered (HWND now exists).
            # We look up by title rather than relying on pywebview internals.
            hwnd = find_window_by_title(title)
            apply_dark_titlebar(hwnd)
            app.start_services()

        def _on_closed() -> None:
            app.shutdown()

        window.events.loaded += _on_loaded
        window.events.closed += _on_closed

        webview.start()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
