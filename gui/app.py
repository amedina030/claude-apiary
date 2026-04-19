"""apiary GUI entry point — `python -m gui.app`.

Phase 1 wiring: spawn a PyWebView window, push session-transcript messages from
the JSONL tail through the JS bridge into the chat renderer. Pty, sidebar, theme
hot-reload, single-instance, packaging come in later phases.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from gui import pty_capture, repo_registry, sidebar_state
from gui.pty_wrapper import PtySpawnError, PtyWrapper
from gui.scribe_aggregator import NoteEntry, ScribeAggregatorService, read_body
from gui.single_instance import SingleInstance
from gui.theme import (
    ThemeWatcher,
    ensure_defaults as ensure_theme_defaults,
    load_launch,
    load_theme,
)
from gui.transcript import (
    Message,
    SessionDiscovery,
    TranscriptTail,
    parse_jsonl_lines,
    snapshot_existing_jsonls,
)
from gui.win_titlebar import apply_dark_titlebar, find_window_by_title

WEB_DIR = Path(__file__).resolve().parent / "web"
INDEX_HTML = WEB_DIR / "index.html"


class GuiBridge:
    """JS → Python surface (frontend calls `pywebview.api.<method>()`)."""

    def __init__(self, app: "App") -> None:
        self._app = app

    def ping(self) -> str:
        return "ok"

    def send_input(self, text: str) -> bool:
        """Send a complete user prompt to claude.

        Two-phase write: text body first, then a tiny pause, then Enter as a
        control character. ConPTY enables bracketed-paste mode by default, and
        a single combined write of "text\\r" can get wrapped in paste markers —
        inside which "\\r" is just a literal newline, not a submit. Splitting
        the writes (and using sendcontrol for the Enter) avoids the bracket so
        claude reliably treats the trailing key as Enter.
        """
        if self._app.pty is None:
            return False
        if not isinstance(text, str):
            return False
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if normalized:
            if not self._app.pty.send_text(normalized):
                return False
        # Small pause so ConPTY doesn't coalesce the terminator into the same write.
        time.sleep(0.03)
        # sendcontrol('m') → 0x0d (Ctrl+M = Enter), routed via a key-event path
        # that doesn't get bracket-wrapped.
        return self._app.pty.send_control("m")

    def send_control(self, ch: str) -> bool:
        """Forward a control character: 'c' → Ctrl+C (0x03), etc."""
        if self._app.pty is None or not isinstance(ch, str) or not ch:
            return False
        return self._app.pty.send_control(ch[:1])

    def send_escape(self) -> bool:
        """Forward Esc (0x1b) to the pty."""
        if self._app.pty is None:
            return False
        return self._app.pty.send_bytes(b"\x1b")

    def send_text(self, text: str) -> bool:
        """Forward raw text without a trailing newline (per-keystroke streaming)."""
        if self._app.pty is None or not isinstance(text, str):
            return False
        return self._app.pty.send_text(text)

    def restart_pty(self) -> bool:
        return self._app.start_pty(restart=True)

    def pty_resize(self, rows, cols) -> bool:
        """Forward xterm.js-computed dimensions to the pty. Best-effort."""
        if self._app.pty is None:
            return False
        try:
            r = int(rows)
            c = int(cols)
        except (TypeError, ValueError):
            return False
        if r <= 0 or c <= 0:
            return False
        self._app.pty.resize(r, c)
        return True

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


class App:
    def __init__(self) -> None:
        self.window = None
        self.pty: Optional[PtyWrapper] = None
        self._tail: Optional[TranscriptTail] = None
        self._discovery: Optional[SessionDiscovery] = None
        self._aggregator: Optional[ScribeAggregatorService] = None
        self._theme_watcher: Optional[ThemeWatcher] = None
        self._js_lock = threading.Lock()
        self._current_path: Optional[Path] = None
        # Pty capture is opt-in via APIARY_GUI_CAPTURE_LABEL. Capture stays
        # on for the lifetime of the process (across pty restarts).
        self._capture: Optional[pty_capture.CaptureWriter] = None
        capture_env = os.environ.get("APIARY_GUI_CAPTURE_LABEL")
        if capture_env is not None:
            path = pty_capture.next_capture_path(capture_env or None)
            self._capture = pty_capture.CaptureWriter(path)
            print(f"[gui] pty capture → {path}", file=sys.stderr)

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

    def _push_message(self, msg: Message) -> None:
        payload = json.dumps(msg.to_dict())
        self._eval(f"window.apiary.onMessage({payload});")

    def _push_messages(self, msgs: list[Message]) -> None:
        if not msgs:
            return
        payload = json.dumps([m.to_dict() for m in msgs])
        self._eval(f"window.apiary.onMessages({payload});")

    def _push_clear(self) -> None:
        self._eval("window.apiary.onClear();")

    def _push_status(self, text: str) -> None:
        self._eval(f"window.apiary.onStatus({json.dumps(text)});")

    def _push_toast(self, text: str, kind: str = "") -> None:
        self._eval(f"window.apiary.onToast({json.dumps(text)}, {json.dumps(kind)});")

    def _push_pty_chunk(self, chunk: str) -> None:
        self._eval(f"window.apiary.onPtyChunk({json.dumps(chunk)});")

    def _push_pty_exit(self, code: int) -> None:
        self._eval(f"window.apiary.onPtyExit({json.dumps(code)});")

    def _push_notes(self, notes: list[NoteEntry], warnings: list[str]) -> None:
        payload = json.dumps([n.to_dict() for n in notes])
        self._eval(f"window.apiary.onNotes({payload});")
        for w in warnings:
            print(f"[scribe] {w}", file=sys.stderr)

    def _push_theme(self, vars_: dict, err: Optional[str]) -> None:
        if err:
            self._push_toast(err, "error")
        self._eval(f"window.apiary.onTheme({json.dumps(vars_)});")

    # --- pty lifecycle ------------------------------------------------------------

    def start_pty(self, restart: bool = False) -> bool:
        if self.pty is not None and not restart and self.pty.is_alive():
            return True
        if self.pty is not None:
            try:
                self.pty.stop()
            except Exception:
                pass
            self.pty = None
        launch = load_launch()
        argv = [launch.get("command", "claude")] + list(launch.get("args", []))
        cwd = launch.get("cwd") or None
        rows = int(launch.get("rows", 40) or 40)
        cols = int(launch.get("cols", 120) or 120)

        # Scrub CLAUDECODE-family env vars so the inner claude starts as a
        # fresh user session. If we inherit them from the outer Claude Code
        # process (which is how a dev launches the GUI via `claude`), the
        # inner claude thinks it's a programmatic sub-invocation and auto-
        # approves every tool — no permission prompts ever render.
        spawn_env = os.environ.copy()
        for key in (
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_EXECPATH",
            "CLAUDE_CODE_SESSION",
            "CLAUDE_CODE_SESSION_ID",
        ):
            spawn_env.pop(key, None)

        # Capture pre-spawn JSONLs so session-discovery ignores any session that
        # existed before the GUI launched its own claude. Also record the spawn
        # moment as a ctime threshold (1s slop for clock skew).
        pre_existing = snapshot_existing_jsonls()
        spawn_time = time.time() - 1.0

        try:
            pty = PtyWrapper(
                argv=argv,
                cwd=cwd,
                env=spawn_env,
                dimensions=(rows, cols),
                on_stdout=self._push_pty_chunk,
                on_exit=self._push_pty_exit,
                capture=self._capture,
            )
            pty.start()
        except PtySpawnError as e:
            self._push_toast(f"Claude Code spawn failed: {e}", "error")
            return False
        self.pty = pty

        # Pin discovery to "JSONLs created after spawn AND not in the pre-existing set."
        # lock_after_first ensures subagent / Task-tool sidechain JSONLs that appear
        # later don't steal focus from the spawned claude's main transcript.
        # Drop any current tail so a fresh JSONL detection clears the chat first.
        if self._discovery is not None:
            self._discovery.set_pin(
                min_ctime=spawn_time,
                exclude_paths=pre_existing,
                lock_after_first=True,
            )
            if self._tail is not None:
                self._tail.stop()
                self._tail = None
            self._current_path = None
            self._push_clear()
            self._push_status("Waiting for spawned session…")
        return True

    # --- transcript wiring --------------------------------------------------------

    def _start_tail(self, path: Path) -> None:
        # Replay full file before tail kicks in so initial render has history.
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._push_status(f"Cannot read {path.name}: {e}")
            return
        history = parse_jsonl_lines(text)
        self._push_status("")
        self._push_messages(history)

        # Tail will re-read from byte 0; fast-forward past the chunk we replayed
        # by seeking to file end before starting the loop. Poll interval is tuned
        # for perceived latency between sending a message and seeing it render —
        # 100ms gives a snappy "appears immediately" feel without burning CPU.
        tail = TranscriptTail(path, on_message=self._push_message, poll_interval=0.1)
        try:
            tail._pos = path.stat().st_size  # private but intentional fast-forward
        except OSError:
            pass
        tail.start()
        self._tail = tail

    def _on_session_switch(self, path: Path) -> None:
        if self._current_path is not None and path == self._current_path:
            return
        self._current_path = path
        # Stop previous tail.
        if self._tail is not None:
            self._tail.stop()
            self._tail = None
        self._push_clear()
        self._push_status(f"Loading session {path.name}…")
        self._start_tail(path)

    def start_services(self) -> None:
        ensure_theme_defaults()
        vars_, theme_err = load_theme()
        self._push_theme(vars_, theme_err)
        self._theme_watcher = ThemeWatcher(on_reload=self._push_theme)
        self._theme_watcher.start()

        # Discovery is constructed first so start_pty() can pin it, but we don't
        # start its polling thread until after the pty spawn — that way the very
        # first discovery tick already sees the spawn-time / pre-existing pin.
        self._discovery = SessionDiscovery(on_switch=self._on_session_switch, poll_interval=2.0)

        repos, repo_err = repo_registry.load()
        if repo_err:
            self._push_toast(repo_err, "error")
        self._aggregator = ScribeAggregatorService(
            repos=repos,
            on_update=self._push_notes,
            interval=5.0,
        )
        self._aggregator.start()

        self.start_pty()
        self._discovery.start()

    def shutdown(self) -> None:
        if self._tail is not None:
            self._tail.stop()
        if self._discovery is not None:
            self._discovery.stop()
        if self._aggregator is not None:
            self._aggregator.stop()
        if self._theme_watcher is not None:
            self._theme_watcher.stop()
        if self.pty is not None:
            self.pty.stop()
        if self._capture is not None:
            self._capture.close()


def main() -> int:
    with SingleInstance() as guard:
        if not guard.acquired:
            print("apiary GUI is already running.", file=sys.stderr)
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

        window = webview.create_window(
            title="apiary",
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
            hwnd = find_window_by_title("apiary")
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
