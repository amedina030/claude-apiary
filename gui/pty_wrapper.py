"""Hidden Claude Code subprocess hosted in a pty (Windows: pywinpty / `winpty` module).

The pty wrapper does not parse pty stdout for messages — JSONL is the sole source
of truth for rendered conversation. The pty stdout is mirrored to a small rolling
buffer so the frontend can show interactive Claude Code UI (permission prompts,
plan-mode banners, ESC-cancellable hints) that never reaches JSONL.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque
from typing import Callable, Optional, Sequence


# Chunk size for send_text. Chosen well under MAX_CANON (4096 on Linux/macOS)
# and the historical Windows ConPTY cooked-mode line-input limit. See
# send_text() docstring.
_SEND_CHUNK_SIZE = 1024


# PASTE-PROBE: temporary truncation diagnostics. Guarded so a missing/broken
# probe module never affects the send path. Remove with the rest of the probes.
try:
    from gui.paste_probe import probe as _paste_probe_raw
    from gui.paste_probe import probe_text as _paste_probe
except Exception:  # pragma: no cover
    def _paste_probe(_hop: str, _text: str) -> None:  # type: ignore[misc]
        pass

    def _paste_probe_raw(_hop: str, _length: int, _head: str = "", _tail: str = "") -> None:  # type: ignore[misc]
        pass


def _resolve_claude_executable(name: str = "claude") -> Optional[str]:
    """Return the absolute path to the `claude` CLI on PATH, or None."""
    found = shutil.which(name)
    if found:
        return found
    # Windows ships .cmd shims for many JS-based CLIs; shutil.which finds them with PATHEXT.
    return None


class PtySpawnError(RuntimeError):
    pass


class PtyWrapper:
    """Spawn `claude` in a pty; stream stdout to a callback; forward bytes to stdin.

    Designed for the frontend to be the sole user-visible surface — pty stdout is
    only mirrored for the pty output strip panel, not parsed for content.
    """

    def __init__(
        self,
        argv: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        dimensions: tuple[int, int] = (40, 120),
        on_stdout: Optional[Callable[[str], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
        ring_size: int = 4096,
        capture=None,
    ) -> None:
        self.argv = list(argv) if argv else ["claude"]
        self.cwd = cwd
        self.env = env
        self.dimensions = dimensions
        self.on_stdout = on_stdout or (lambda _s: None)
        self.on_exit = on_exit or (lambda _code: None)
        self._ring: deque[str] = deque(maxlen=ring_size)
        self._proc = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Monotonic timestamp of the most recent non-empty stdout chunk. Used by
        # wait_for_quiet() to time a submit CR after a bracketed paste. 0.0 means
        # no output has been seen yet.
        self._last_out_at = 0.0
        # Optional raw-bytes sink (gui.pty_capture.CaptureWriter). Writes are
        # pre-decode so captures preserve exact terminal fidelity for the
        # prompt-detector fixtures.
        self._capture = capture

    @property
    def buffer(self) -> str:
        return "".join(self._ring)

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

        # Resolve `claude` exe. shutil.which honors PATHEXT, so .cmd/.exe/.bat all work.
        argv = list(self.argv)
        resolved = _resolve_claude_executable(argv[0])
        if resolved is None:
            raise PtySpawnError(
                f"could not find {argv[0]!r} on PATH — install Claude Code or set launch.json command"
            )
        argv[0] = resolved

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
            with self._lock:
                self._ring.append(chunk)
            try:
                self.on_stdout(chunk)
            except Exception:
                pass
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
        if not text:
            return True
        _paste_probe("pty_send", text)  # PASTE-PROBE: bytes entering the pty layer
        try:
            chunks = 0
            short = 0  # PASTE-PROBE: count of chunks where write() reported a short count
            for i in range(0, len(text), _SEND_CHUNK_SIZE):
                chunk = text[i:i + _SEND_CHUNK_SIZE]
                # PASTE-PROBE: pywinpty.write returns the number of BYTES written.
                # We compare against the chunk's UTF-8 byte length; a short count
                # means the native agent accepted only part of the chunk and the
                # remainder is silently dropped (the discarded-return-value bug).
                want = len(chunk.encode("utf-8"))
                got = proc.write(chunk)
                try:
                    got = int(got)
                except (TypeError, ValueError):
                    got = -1
                if got != want:
                    short += 1
                    _paste_probe_raw(
                        f"pty_short(chunk={chunks},want={want},got={got})",
                        len(chunk), chunk[:24], chunk[-24:] if len(chunk) > 24 else "",
                    )
                chunks += 1
            _paste_probe(f"pty_done(chunks={chunks},short={short})", text)  # PASTE-PROBE: loop completed
            return True
        except Exception:
            return False

    def send_control(self, ch: str) -> bool:
        """Send a control character (e.g. 'c' → Ctrl+C → 0x03)."""
        proc = self._proc
        if proc is None or not proc.isalive():
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
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except Exception:
            pass
