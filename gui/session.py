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

from gui.ask_prompt import AskPromptWatcher
from gui.file_refs import FileRefs
from gui.permission_mcp import SESSION_ID_ENV, permission_tool_arg, write_mcp_config
from gui.pty_capture import CaptureWriter
from gui.pty_wrapper import PtySpawnError, PtyWrapper
from gui.pty_wrapper import contains_ctrl_c
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
    with path separators, dots AND spaces replaced by ``-``. Notably this
    differs from ``core.utils.project.project_key_from_path`` which
    preserves dots — keep this local so we don't risk changing apiary's
    on-disk scribe layout for existing users.

    Example: ``C:\\Users\\user\\.claude\\projects\\claude-apiary`` →
    ``C--Users-user--claude-projects-claude-apiary``.

    Spaces matter: a repo at ``D:\\Professional\\Hexworld Rebuilt`` gets a
    transcript dir of ``D--Professional-Hexworld-Rebuilt``. Leaving the
    space intact pointed SessionDiscovery at a directory that can never
    exist, so the tab found no JSONL and rendered no assistant messages,
    no model name, and zero token counts — while the pty pane worked fine,
    since that path never touches the transcript. Underscores are NOT
    replaced (cf. ``D--Professional-job_search``).
    """
    p = Path(cwd).resolve()
    if p.drive:
        drive = p.drive.rstrip(":\\")
        rest = str(p).replace(p.drive + "\\", "")
        rest = _replace_key_seps(rest)
        return f"{drive}--{rest}"
    rest = str(p).lstrip("/")
    return "-" + _replace_key_seps(rest)


def _replace_key_seps(rest: str) -> str:
    """Collapse every char Claude Code maps to ``-`` in a project key."""
    for ch in ("\\", "/", ".", " "):
        rest = rest.replace(ch, "-")
    return rest


def _projects_root_for(cwd: Path) -> Path:
    """Return the per-cwd ~/.claude/projects/<key>/ directory.

    Claude Code writes its JSONL transcripts under this subdir (one per
    session). Scoping SessionDiscovery here keeps tab A's discovery blind
    to tab B's JSONLs so they can never steal each other's focus.
    """
    return _CLAUDE_PROJECTS_DIR / _claude_code_project_key(cwd)


_CLEAR_CMD_RE = re.compile(r"^\s*/clear(\s|$)")

# Terminal bracketed-paste markers (DECSET 2004). Wrapping a prompt body in
# these makes the Claude Code CLI absorb it in *paste mode* — accumulating the
# bytes without per-keystroke processing — instead of treating a multi-KB paste
# as raw typing (which backed up its read loop until the Windows ConPTY input
# buffer overflowed and silently dropped the head). The CLI enables mode 2004
# and parses these markers on stdin; this mirrors its own pty-injection pattern.
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# Env vars that make an inner claude think it's a sub-invocation and auto-
# approve every tool. Must be scrubbed before pty spawn.
_CLAUDE_SUBPROC_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION",
    "CLAUDE_CODE_SESSION_ID",
)

# Set to "1" in the spawned claude's env so the startup hook can tell the
# session it's running inside the GUI. Read by core/hooks/startup_prompt_hook.py
# (as a literal — core/ must not import from gui/).
GUI_SESSION_ENV = "APIARY_GUI_SESSION"


def replay_cut(raw: bytes) -> int:
    """Byte offset just past the last complete line in *raw* (0 if none).

    The attach path replays ``raw[:cut]`` and starts the tail at ``cut`` so
    a record torn by a mid-write read is neither half-rendered nor lost.
    """
    return raw.rfind(b"\n") + 1


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
        on_ask_prompt: Optional[Callable] = None,
        on_ask_prompt_resolved: Optional[Callable] = None,
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
        # Per-tab drag-drop / paste registry, keyed by this tab's session_id so
        # staged files belong to this tab alone (never leak to another tab's
        # message). Paths only here — no I/O until the first add.
        self.file_refs = FileRefs(scope=self.session_id)
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
        # Transcript-sourced AskUserQuestion banner. The watcher reads the
        # structured tool_use/tool_result records (no xterm scraping), and these
        # callbacks route the pending prompt / its resolution to the active tab.
        _noop = lambda *_a, **_k: None
        _push_ask = on_ask_prompt or _noop
        _push_ask_resolved = on_ask_prompt_resolved or _noop
        self.ask_watcher = AskPromptWatcher(
            on_prompt=lambda payload: _push_ask(payload, sid),
            on_resolved=lambda tool_use_id: _push_ask_resolved(tool_use_id, sid),
        )
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
        # Scribe aggregator scoped to this session's cwd only. Unregistered
        # repos with no pointer file simply yield an empty notes list.
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
        # Mark this as a GUI-hosted session. claude inherits this env, and so
        # do its hook subprocesses, so the startup hook can tell the session
        # it's running inside the GUI rather than a raw terminal.
        spawn_env[GUI_SESSION_ENV] = "1"
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
        # Read the bytes exactly once and start the tail at that byte count.
        # Reading, replaying, and only then fast-forwarding to stat().st_size
        # lost every record claude appended in between — routinely tens of
        # ms of writes while it streams its first turn (review gui #2).
        try:
            raw = path.read_bytes()
        except OSError as e:
            self._on_status(f"Cannot read {path.name}: {e}")
            return
        # Replay only whole lines: a read that tears a record mid-write
        # would otherwise drop its head here and hand the tail only its
        # unparseable remainder. The partial line is left for the tail.
        cut = replay_cut(raw)
        text = raw[:cut].decode("utf-8", errors="replace")
        history = parse_jsonl_lines(text)
        self._on_status("")
        self._on_messages(history)
        # Replay raw history into the subagent tracker so it learns about
        # Agent spawns and tool_results that happened before the GUI attached.
        # The fast-forward below means new records otherwise miss everything
        # prior to now.
        tracker = self.subagent_tracker
        records = list(iter_jsonl_records(text))
        if tracker is not None:
            for rec in records:
                tracker.note_parent_record(rec)
        # Replay history into the ask-watcher silently so an already-answered
        # prompt doesn't flash a banner; a still-pending one is surfaced once.
        # Reset first so a prior transcript's pending state (e.g. across /clear)
        # can't leak into this one.
        self.ask_watcher.reset()
        self.ask_watcher.replay(records)

        def _on_record(rec):
            if tracker is not None:
                try:
                    tracker.note_parent_record(rec)
                except Exception:
                    pass
            self.ask_watcher.note_record(rec)

        tail = TranscriptTail(
            path,
            on_message=self._on_message,
            poll_interval=0.1,
            on_record=_on_record,
            start_at=cut,
        )
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
        """Send a complete user prompt: the body as a bracketed paste, then
        Enter as a separate control char.

        The body is wrapped in bracketed-paste markers (ESC[200~ ... ESC[201~)
        so the CLI absorbs it in paste mode rather than as raw keystrokes. Raw
        keystrokes made the CLI re-render per character; its read loop backed up
        and the Windows ConPTY input buffer overflowed, silently dropping the
        head of large pastes. The Enter is sent as a control char so it lands
        *after* paste mode ends (ESC[201~) and submits, rather than being folded
        into the pasted text. Mirrors the CLI's own pty-injection pattern.
        """
        if self.pty is None or not isinstance(text, str) or contains_ctrl_c(text):
            return False
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if normalized:
            baseline = self.pty.last_output_at
            payload = f"{_PASTE_START}{normalized}{_PASTE_END}"
            if not self.pty.send_text(payload):
                return False
            # Hold the submit CR until the CLI has finished ingesting the paste.
            # Sent too soon, the CR races the paste collapse and is swallowed,
            # forcing the user to press Enter twice. Wait for pty output to go
            # quiet (machine-speed-adaptive) instead of a fixed sleep; the
            # timeout is a safety floor if the paste produces no output.
            self.pty.wait_for_quiet(after=baseline)
        else:
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

    # Every input path refuses a raw Ctrl+C: it kills the claude session
    # rather than interrupting the turn. Interrupt = ESC then Ctrl+U. The
    # pty layer refuses it as well; the duplication is deliberate so that a
    # future pty backend cannot lose the rule.

    def send_text(self, text: str) -> bool:
        if self.pty is None or not isinstance(text, str) or contains_ctrl_c(text):
            return False
        return self.pty.send_text(text)

    def send_control(self, ch: str) -> bool:
        if self.pty is None or not isinstance(ch, str) or not ch:
            return False
        if ch[0].lower() == "c" or contains_ctrl_c(ch):
            return False
        return self.pty.send_control(ch[:1])

    def send_escape(self) -> bool:
        if self.pty is None:
            return False
        return self.pty.send_bytes(b"\x1b")

    def send_bytes(self, values) -> bool:
        """Forward an exact byte sequence to the pty.

        Crosses the JS bridge as a list of ints (0-255), e.g. arrow-up =
        ``[27, 91, 65]`` (ESC [ A). Used by the arrow-key menu driver, which
        needs precise control bytes with no text-mode/IME transformation.
        Rejects out-of-range or non-int values rather than sending garbage.
        """
        if self.pty is None:
            return False
        if not isinstance(values, (list, tuple)) or not values:
            return False
        try:
            raw = bytes(int(v) for v in values)
        except (TypeError, ValueError):
            return False
        if contains_ctrl_c(raw):
            return False
        return self.pty.send_bytes(raw)

    def pty_resize(self, rows: int, cols: int) -> bool:
        if self.pty is None:
            return False
        self.pty.resize(rows, cols)
        return True
