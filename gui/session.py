"""One tab's worth of state: a claude pty subprocess, its transcript tail,
the session-discovery that picks up post-/clear JSONLs, and the working
directory they're all pinned to.

Extracted from ``gui.app`` so ``App`` can hold a list of Sessions (one per
tab) without each piece of per-session state living at the App level. All
UI-push side effects go through caller-supplied callbacks so ``App`` can
route them to the active tab (and later, to a specific tab id).
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from gui.permission_mcp import SESSION_ID_ENV, permission_tool_arg, write_mcp_config
from gui.pty_capture import CaptureWriter
from gui.pty_wrapper import PtySpawnError, PtyWrapper
from gui.scribe_aggregator import ScribeAggregatorService, aggregate
from gui.subagent_tracker import SubagentTracker
from gui.transcript import (
    Message,
    SessionDiscovery,
    TranscriptTail,
    iter_jsonl_records,
    parse_jsonl_lines,
    snapshot_existing_jsonls,
)

_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _claude_code_project_key(cwd: Path) -> str:
    """Mirror Claude Code's cwd → project-key transform.

    Claude Code stores each session's JSONL under
    ``~/.claude/projects/<key>/`` where ``<key>`` is the absolute cwd path
    with path separators AND dots replaced by ``-``. Notably this differs
    from ``core.utils.project.project_key_from_path`` which preserves dots
    — keep this local so we don't risk changing apiary's on-disk scribe
    layout for existing users.

    Example: ``C:\\Users\\amedi\\.claude\\projects\\claude-apiary`` →
    ``C--Users-amedi--claude-projects-claude-apiary``.
    """
    p = Path(cwd).resolve()
    if p.drive:
        drive = p.drive.rstrip(":\\")
        rest = str(p).replace(p.drive + "\\", "")
        rest = rest.replace("\\", "-").replace("/", "-").replace(".", "-")
        return f"{drive}--{rest}"
    rest = str(p).lstrip("/")
    return "-" + rest.replace("/", "-").replace(".", "-")


def _projects_root_for(cwd: Path) -> Path:
    """Return the per-cwd ~/.claude/projects/<key>/ directory.

    Claude Code writes its JSONL transcripts under this subdir (one per
    session). Scoping SessionDiscovery here keeps tab A's discovery blind
    to tab B's JSONLs so they can never steal each other's focus.
    """
    return _CLAUDE_PROJECTS_DIR / _claude_code_project_key(cwd)


_CLEAR_CMD_RE = re.compile(r"^\s*/clear(\s|$)")

# Env vars that make an inner claude think it's a sub-invocation and auto-
# approve every tool. Must be scrubbed before pty spawn.
_CLAUDE_SUBPROC_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION",
    "CLAUDE_CODE_SESSION_ID",
)


class Session:
    """A single claude subprocess + transcript pipeline keyed to one cwd."""

    def __init__(
        self,
        cwd: Path,
        on_message: Callable,
        on_messages: Callable,
        on_clear: Callable,
        on_status: Callable,
        on_pty_chunk: Callable,
        on_pty_exit: Callable,
        on_toast: Callable,
        on_notes: Callable,
        on_agents: Callable,
        capture: Optional[CaptureWriter] = None,
        command: str = "claude",
        args: Optional[list] = None,
        rows: int = 40,
        cols: int = 120,
        accept_edits: bool = False,
    ) -> None:
        # Callbacks receive (payload, session_id) so App can route UI pushes
        # to the right tab. session_id is the first field assigned so the
        # callback bindings below can close over the right value.
        self.session_id = str(uuid.uuid4())
        self.cwd = Path(cwd)
        sid = self.session_id
        self._on_message = lambda msg: on_message(msg, sid)
        self._on_messages = lambda msgs: on_messages(msgs, sid)
        self._on_clear = lambda: on_clear(sid)
        self._on_status = lambda text: on_status(text, sid)
        self._on_pty_chunk = lambda chunk: on_pty_chunk(chunk, sid)
        self._on_pty_exit = lambda code: on_pty_exit(code, sid)
        self._on_toast = lambda text, kind="": on_toast(text, kind, sid)
        self._on_notes = lambda notes, warnings: on_notes(notes, warnings, sid)
        self._on_agents = lambda agents: on_agents(agents, sid)
        self._capture = capture
        self._command = command
        self._args = list(args or [])
        self._rows = rows
        self._cols = cols
        # Per-tab auto-accept-edits toggle (T-2026-176). Spawn-time flag that
        # prepends --permission-mode acceptEdits; flipping it mid-session
        # requires a pty restart.
        self.accept_edits = bool(accept_edits)

        self.pty: Optional[PtyWrapper] = None
        self.discovery: Optional[SessionDiscovery] = None
        self.tail: Optional[TranscriptTail] = None
        self.current_path: Optional[Path] = None
        self.aggregator: Optional[ScribeAggregatorService] = None
        self.subagent_tracker: Optional[SubagentTracker] = None

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        """Spawn pty + discovery + scribe aggregator. Returns False on pty
        spawn failure (aggregator still runs; it's just a scribe reader)."""
        self.discovery = SessionDiscovery(
            on_switch=self._on_session_switch,
            projects_root=_projects_root_for(self.cwd),
            poll_interval=2.0,
        )
        if not self._start_pty():
            return False
        self.discovery.start()
        # Scribe aggregator scoped to this session's cwd only. Non-apiary cwds
        # simply yield an empty notes list (no .apiary/scribe/ to read).
        self.aggregator = ScribeAggregatorService(
            repos=[self.cwd],
            on_update=self._on_notes,
            interval=5.0,
        )
        self.aggregator.start()
        self.subagent_tracker = SubagentTracker(
            session_jsonl_fn=lambda: self.current_path,
            session_id_fn=lambda: self.session_id,
            on_update=lambda agents, _sid: self._on_agents(agents),
            poll_interval=2.0,
        )
        self.subagent_tracker.start()
        return True

    def flush_notes(self) -> None:
        """Aggregate once and push immediately. Called by App.switch_to so the
        newly-active tab's sidebar doesn't wait for the next 5s tick."""
        try:
            notes, warnings = aggregate([self.cwd])
            self._on_notes(notes, warnings)
        except Exception:
            pass

    def restart_pty(self) -> bool:
        return self._start_pty(restart=True)

    def stop(self) -> None:
        if self.subagent_tracker is not None:
            try:
                self.subagent_tracker.stop()
            except Exception:
                pass
            self.subagent_tracker = None
        if self.aggregator is not None:
            try:
                self.aggregator.stop()
            except Exception:
                pass
            self.aggregator = None
        if self.tail is not None:
            try:
                self.tail.stop()
            except Exception:
                pass
            self.tail = None
        if self.discovery is not None:
            try:
                self.discovery.stop()
            except Exception:
                pass
            self.discovery = None
        if self.pty is not None:
            try:
                self.pty.stop()
            except Exception:
                pass
            self.pty = None

    # --- pty spawn ------------------------------------------------------------

    def _start_pty(self, restart: bool = False) -> bool:
        if self.pty is not None and not restart and self.pty.is_alive():
            return True
        if self.pty is not None:
            try:
                self.pty.stop()
            except Exception:
                pass
            self.pty = None

        # Prepend --permission-mode acceptEdits when the per-tab "auto-accept
        # edits" toggle is on. Spawn-time only: flipping the toggle mid-session
        # requires a pty restart (App.set_session_accept_edits handles that).
        extra = ["--permission-mode", "acceptEdits"] if self.accept_edits else []
        # Opt-in: route permission prompts through a local MCP server instead
        # of scraping the TUI banner. Enable with APIARY_PERMISSION_MCP=1; the
        # GUI starts a loopback HTTP bridge so claude's spawned MCP subprocess
        # can round-trip decisions back to the webview. See scribe C-2026-36.
        mcp_on = os.environ.get("APIARY_PERMISSION_MCP") == "1"
        if mcp_on:
            cfg_path = write_mcp_config()
            extra += [
                "--mcp-config", str(cfg_path),
                "--permission-prompt-tool", permission_tool_arg(),
            ]
        argv = [self._command] + extra + self._args
        spawn_env = os.environ.copy()
        for key in _CLAUDE_SUBPROC_ENV:
            spawn_env.pop(key, None)
        # Tag the MCP subprocess (spawned by claude, inherits this env) with
        # the owning session_id so the GUI can route incoming permission
        # prompts to the correct tab. See scribe C-2026-36 follow-up #4.
        if mcp_on:
            spawn_env[SESSION_ID_ENV] = self.session_id

        # Pre-spawn snapshot scoped to THIS cwd's projects dir — matches the
        # scope of our SessionDiscovery so exclude_paths semantics line up.
        # 1s slop on spawn_time covers clock skew.
        pre_existing = snapshot_existing_jsonls(_projects_root_for(self.cwd))
        spawn_time = time.time() - 1.0

        try:
            pty = PtyWrapper(
                argv=argv,
                cwd=str(self.cwd) if self.cwd else None,
                env=spawn_env,
                dimensions=(self._rows, self._cols),
                on_stdout=self._on_pty_chunk,
                on_exit=self._on_pty_exit,
                capture=self._capture,
            )
            pty.start()
        except PtySpawnError as e:
            self._on_toast(f"Claude Code spawn failed: {e}", "error")
            return False
        self.pty = pty

        if self.discovery is not None:
            self.discovery.set_pin(
                min_ctime=spawn_time,
                exclude_paths=pre_existing,
                lock_after_first=True,
            )
            if self.tail is not None:
                self.tail.stop()
                self.tail = None
            self.current_path = None
            self._on_clear()
            self._on_status("Waiting for spawned session…")
        return True

    # --- transcript wiring ----------------------------------------------------

    def _start_tail(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._on_status(f"Cannot read {path.name}: {e}")
            return
        history = parse_jsonl_lines(text)
        self._on_status("")
        self._on_messages(history)
        # Replay raw history into the subagent tracker so it learns about
        # Agent spawns and tool_results that happened before the GUI attached.
        # The fast-forward below means new records otherwise miss everything
        # prior to now.
        tracker = self.subagent_tracker
        if tracker is not None:
            for rec in iter_jsonl_records(text):
                tracker.note_parent_record(rec)
        on_record = tracker.note_parent_record if tracker is not None else None
        tail = TranscriptTail(
            path,
            on_message=self._on_message,
            poll_interval=0.1,
            on_record=on_record,
        )
        try:
            tail._pos = path.stat().st_size  # private but intentional fast-forward
        except OSError:
            pass
        tail.start()
        self.tail = tail

    def _on_session_switch(self, path: Path) -> None:
        if self.current_path is not None and path == self.current_path:
            return
        self.current_path = path
        if self.tail is not None:
            self.tail.stop()
            self.tail = None
        self._on_clear()
        self._on_status(f"Loading session {path.name}…")
        self._start_tail(path)

    def on_clear_requested(self) -> None:
        """Handle /clear: re-pin discovery so the fresh post-clear JSONL takes
        over. Meant to run on a background thread so the pty send path isn't
        blocked.
        """
        if self.discovery is None:
            return
        # Claude needs ~300-800ms to finalize the old JSONL and create a new
        # one; 0.5s clears it without feeling laggy.
        time.sleep(0.5)
        spawn_time = time.time() - 1.0
        self.discovery.set_pin(
            min_ctime=spawn_time,
            lock_after_first=True,
        )
        if self.tail is not None:
            self.tail.stop()
            self.tail = None
        self.current_path = None
        self._on_clear()
        self._on_status("Waiting for post-clear session…")

    # --- pty input surface ----------------------------------------------------

    def send_input(self, text: str) -> bool:
        """Send a complete user prompt: text body, then Enter as a control
        char (avoids ConPTY bracketed-paste wrapping the \\r).
        """
        if self.pty is None or not isinstance(text, str):
            return False
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if normalized:
            if not self.pty.send_text(normalized):
                return False
        time.sleep(0.03)
        ok = self.pty.send_control("m")
        if ok and _CLEAR_CMD_RE.match(normalized):
            # Re-pin discovery on /clear so the post-clear JSONL gets picked up.
            # Background thread so this call returns immediately.
            threading.Thread(
                target=self.on_clear_requested,
                name="clear-reset",
                daemon=True,
            ).start()
        return ok

    def send_text(self, text: str) -> bool:
        if self.pty is None or not isinstance(text, str):
            return False
        return self.pty.send_text(text)

    def send_control(self, ch: str) -> bool:
        if self.pty is None or not isinstance(ch, str) or not ch:
            return False
        return self.pty.send_control(ch[:1])

    def send_escape(self) -> bool:
        if self.pty is None:
            return False
        return self.pty.send_bytes(b"\x1b")

    def pty_resize(self, rows: int, cols: int) -> bool:
        if self.pty is None:
            return False
        self.pty.resize(rows, cols)
        return True
