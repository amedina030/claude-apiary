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
from collections import deque
from typing import Callable, Optional, Sequence


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

    @property
    def buffer(self) -> str:
        return "".join(self._ring)

    def is_alive(self) -> bool:
        try:
            return self._proc is not None and self._proc.isalive()
        except Exception:
            return False

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
        """Write text characters to the pty. Returns True on success."""
        proc = self._proc
        if proc is None or not proc.isalive():
            return False
        try:
            proc.write(text)
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
