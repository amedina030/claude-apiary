"""Hidden Claude Code subprocess hosted in a pty (Windows: pywinpty / `winpty` module).

The pty wrapper does not parse pty stdout for messages — JSONL is the sole source
of truth for rendered conversation. Stdout is streamed straight to the frontend's
terminal pane so it can show interactive Claude Code UI (permission prompts,
plan-mode banners, ESC-cancellable hints) that never reaches JSONL; nothing is
buffered on the Python side.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Callable, Optional, Sequence

# Chunk size for send_text. Chosen well under MAX_CANON (4096 on Linux/macOS)
# and the historical Windows ConPTY cooked-mode line-input limit. See
# send_text() docstring.
_SEND_CHUNK_SIZE = 1024

# Raw Ctrl+C (ETX). The GUI must never send it: Claude Code treats it as
# "kill the session", not "interrupt the turn". The sanctioned interrupt is
# ESC (stop the turn) followed by Ctrl+U (clear the composer). Every send
# path below rejects it so the rule is structural, not a comment in app.js
# (review gui #4, memory feedback_gui_never_kill_session).
CTRL_C = "\x03"


def contains_ctrl_c(data) -> bool:
    """True if *data* (str or bytes) carries a raw Ctrl+C anywhere."""
    if isinstance(data, bytes):
        return b"\x03" in data
    return isinstance(data, str) and CTRL_C in data


def _kill_process_tree(pid: int) -> None:
    """Best-effort kill of *pid* and every descendant.

    pywinpty's terminate() is TerminateProcess on the direct child only. When
    claude was spawned through an npm ``cmd /c claude.cmd`` shim that child is
    cmd.exe and the node grandchild running Claude Code survives, still
    holding the pty's output pipe (review gui #3). Capability-detected, not
    OS-branched: ``taskkill /T`` where it exists, ``os.killpg`` where it does.
    """
    taskkill = shutil.which("taskkill")
    if taskkill:
        try:
            subprocess.run(
                [taskkill, "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False,
                # The windowed (console=False) build would otherwise flash a
                # console for every tab close.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg and getpgid:
        try:
            import signal
            killpg(getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _resolve_claude_command(name: str = "claude") -> Optional[list[str]]:
    """Resolve the `claude` CLI to a spawnable argv prefix, or None if absent.

    Windows installs Claude Code via npm as a batch shim (`claude.cmd`). Handing
    a `.cmd`/`.bat` straight to ConPTY/winpty fails with WinError 193 ("%1 is not
    a valid Win32 application"), because a batch file is not an executable image.
    So:

    - Prefer a real `claude.exe` if one is on PATH (the native installer ships
      one), even when a shim would otherwise sort first.
    - If only a batch shim is found, wrap it through `cmd.exe /c` so it spawns.
    - On non-Windows, return the resolved path as-is.

    Resolves T-2026-232.
    """
    # Prefer a real executable on Windows even if a shim sorts first under PATHEXT.
    if os.name == "nt" and not name.lower().endswith((".exe", ".cmd", ".bat", ".ps1")):
        exe = shutil.which(name + ".exe")
        if exe:
            return [exe]
    found = shutil.which(name)
    if not found:
        return None
    if os.name == "nt" and os.path.splitext(found)[1].lower() in (".cmd", ".bat"):
        return ["cmd", "/c", found]
    return [found]


class PtySpawnError(RuntimeError):
    pass


class PtyWrapper:
    """Spawn `claude` in a pty; stream stdout to a callback; forward bytes to stdin.

    Designed for the frontend to be the sole user-visible surface — pty stdout is
    only forwarded to the frontend's terminal pane, not parsed for content.
    """

    def __init__(
        self,
        argv: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        dimensions: tuple[int, int] = (40, 120),
        on_stdout: Optional[Callable[[str], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
        capture=None,
    ) -> None:
        self.argv = list(argv) if argv else ["claude"]
        self.cwd = cwd
        self.env = env
        self.dimensions = dimensions
        self.on_stdout = on_stdout or (lambda _s: None)
        self.on_exit = on_exit or (lambda _code: None)
        self._proc = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Monotonic timestamp of the most recent non-empty stdout chunk. Used by
        # wait_for_quiet() to time a submit CR after a bracketed paste. 0.0 means
        # no output has been seen yet.
        self._last_out_at = 0.0
        # Optional raw-bytes sink (gui.pty_capture.CaptureWriter). Writes are
        # pre-decode so captures preserve exact terminal fidelity for the
        # prompt-detector fixtures.
        self._capture = capture

    def is_alive(self) -> bool:
        try:
            return self._proc is not None and self._proc.isalive()
        except Exception:
            return False

    @property
    def last_output_at(self) -> float:
        """Monotonic timestamp of the most recent non-empty stdout chunk, or
        0.0 if no output has been observed yet. Capture this before a write to
        use as the ``after`` baseline for :meth:`wait_for_quiet`."""
        return self._last_out_at

    def wait_for_quiet(
        self,
        after: float,
        quiet: float = 0.12,
        timeout: float = 1.0,
        poll: float = 0.02,
    ) -> bool:
        """Block until stdout has been quiet for ``quiet`` seconds following new
        output that arrived after the ``after`` timestamp, or until ``timeout``
        seconds elapse. Returns True if quiet was reached, False on timeout.

        Used to time a submit CR after a bracketed paste: the CLI must finish
        ingesting and collapsing a multi-KB paste before a CR registers as
        "submit" rather than being swallowed by the paste render, which forced
        the user to press Enter twice. We first wait for the paste to *start*
        producing output (last_output_at advancing past ``after``) so an idle
        prompt's stale quiet doesn't trigger an immediate, too-early CR; then we
        wait for that output to settle. Adapts to machine speed where a fixed
        sleep cannot. The timeout is a safety floor for the no-output case.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        saw_activity = False
        while True:
            now = time.monotonic()
            last = self._last_out_at
            if last > after:
                saw_activity = True
            if saw_activity and (now - last) >= quiet:
                return True
            if now >= deadline or self._stop.is_set():
                return False
            self._stop.wait(min(poll, max(0.0, deadline - now)))

    def start(self) -> None:
        # Late import: keeps gui/transcript.py importable on non-Windows / no-pywinpty hosts.
        try:
            from winpty import PtyProcess  # type: ignore
        except ImportError as e:
            raise PtySpawnError(f"pywinpty not installed: {e}") from e

        # Resolve `claude` to a spawnable argv prefix — prefers a real .exe and
        # wraps a .cmd/.bat shim through cmd.exe (avoids WinError 193).
        argv = list(self.argv)
        prefix = _resolve_claude_command(argv[0])
        if prefix is None:
            raise PtySpawnError(
                f"could not find {argv[0]!r} on PATH. Install Claude Code "
                f"(https://claude.ai/claude-code), or set an explicit 'command' "
                f"in launch.json."
            )
        argv = prefix + argv[1:]

        try:
            self._proc = PtyProcess.spawn(
                argv,
                cwd=self.cwd,
                env=self.env or os.environ.copy(),
                dimensions=self.dimensions,
            )
        except Exception as e:
            raise PtySpawnError(f"PtyProcess.spawn failed: {e}") from e

        self._reader_thread = threading.Thread(
            target=self._read_loop, name="pty-reader", daemon=True
        )
        self._reader_thread.start()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        while not self._stop.is_set():
            try:
                chunk = proc.read(4096)
            except EOFError:
                break
            except Exception:
                break
            if chunk is None or chunk == "":
                # Brief idle — without this we'd busy-loop on a closed pty.
                if not proc.isalive():
                    break
                self._stop.wait(0.05)
                continue
            self._last_out_at = time.monotonic()
            # Capture raw chunk (pre-decode) for fidelity — ANSI escapes,
            # control bytes, and any invalid UTF-8 survive to the fixture file.
            if self._capture is not None:
                try:
                    self._capture.write(chunk)
                except Exception:
                    pass
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            try:
                self.on_stdout(chunk)
            except Exception:
                pass
        if self._stop.is_set():
            # Deliberate stop(): the UI asked for this; an "exited (code -1)"
            # toast here would be a lie.
            return
        # Process exited.
        code = -1
        try:
            code = proc.exitstatus if proc.exitstatus is not None else -1
        except Exception:
            pass
        try:
            self.on_exit(code)
        except Exception:
            pass

    def send_text(self, text: str) -> bool:
        """Write text characters to the pty in chunks. Returns True on success.

        Long writes (multi-KB pastes) are split into ``_SEND_CHUNK_SIZE``-char
        segments. A single ``proc.write`` of a multi-KB string can be silently
        truncated by ConPTY's input handling on Windows and by canonical-mode
        line buffering (MAX_CANON = 4096) on POSIX — the *tail* survives and
        the head is dropped, so a 5KB paste arrives starting mid-word.
        Chunking each write below any plausible buffer cap avoids this.
        """
        proc = self._proc
        if proc is None or not proc.isalive():
            return False
        if contains_ctrl_c(text):
            return False
        if not text:
            return True
        try:
            for i in range(0, len(text), _SEND_CHUNK_SIZE):
                proc.write(text[i:i + _SEND_CHUNK_SIZE])
            return True
        except Exception:
            return False

    def send_control(self, ch: str) -> bool:
        """Send a control character (e.g. 'm' → Enter, 'u' → Ctrl+U).

        'c' (Ctrl+C) is refused — see ``CTRL_C``.
        """
        proc = self._proc
        if proc is None or not proc.isalive():
            return False
        if not ch or ch[0].lower() == "c" or contains_ctrl_c(ch):
            return False
        try:
            proc.sendcontrol(ch)
            return True
        except Exception:
            return False

    def send_bytes(self, raw: bytes) -> bool:
        """Forward arbitrary bytes (e.g. Esc = b'\\x1b'). pywinpty.write takes str only,
        so we decode latin-1 round-trip to push raw bytes through.
        """
        proc = self._proc
        if proc is None or not proc.isalive():
            return False
        if contains_ctrl_c(raw):
            return False
        try:
            proc.write(raw.decode("latin-1"))
            return True
        except Exception:
            return False

    def resize(self, rows: int, cols: int) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.setwinsize(rows, cols)
        except Exception:
            pass

    def stop(self) -> None:
        """Stop the child and release the pty.

        terminate() alone left two things behind (review gui #3): the
        pseudoconsole (only closed by PtyProcess.__del__, which the reader
        thread pinned) and, on npm installs, the node grandchild behind the
        ``cmd /c`` shim. Now: kill the whole tree first (while the direct
        child is alive and its children can still be enumerated), then
        terminate, then close the pty (which also unblocks the reader).
        """
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        pid = getattr(proc, "pid", None)
        # Tree first, while the direct child is still alive: once it is
        # gone its children are re-parented and ``taskkill /T`` can no
        # longer find them (verified by StopKillsGrandchildTest).
        if isinstance(pid, int) and pid > 0:
            _kill_process_tree(pid)
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except Exception:
            pass
        # pywinpty's read() is a blocking recv() on a loopback socket and its
        # close() only close()s it — a recv blocked in another thread does not
        # wake for that. shutdown() does, so the reader sees EOF and exits.
        fileobj = getattr(proc, "fileobj", None)
        if fileobj is not None:
            try:
                fileobj.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
        try:
            proc.close(force=True)
        except Exception:
            pass
        reader = self._reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        self._proc = None
