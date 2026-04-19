"""Raw pty output capture for building prompt-detector fixtures.

When a `CaptureWriter` is handed to `PtyWrapper`, every chunk the pty yields is
written verbatim (pre-decode) to a timestamped file under
`~/.claude/apiary_gui/captures/`. Files are binary because pty output contains
raw ANSI escapes, control bytes, cursor-movement sequences, and may include
partial/invalid UTF-8 in edge cases — text-mode would lose fidelity.

Typical workflow for adding a new prompt-handler case:

    1. Launch the GUI with capture on:
         python -m gui.capture_session --label tool_permission
    2. Reproduce the interactive UI you want handled.
    3. Close the window; the `.bin` file under the captures directory is the
       fixture for `gui/test_prompt_detector.py`.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
from pathlib import Path
from typing import Optional

from gui.theme import THEME_DIR

CAPTURES_DIR = THEME_DIR / "captures"

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# ANSI-stripping helpers for inspecting captures. The patterns are crude —
# they do NOT emulate cursor movements — so stripped output still contains
# repaint trails. For a faithful reconstruction the capture must be replayed
# through a real terminal emulator (e.g. xterm.js). This is good enough for
# "what's roughly in the stream" reconnaissance when building fixtures.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_ESC = re.compile(r"\x1b[@-_]")


def strip_ansi(text: str) -> str:
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_ESC.sub("", text)
    return text


def _sanitize_label(label: str) -> str:
    clean = _LABEL_SAFE.sub("-", label).strip("-")
    return clean or "capture"


def next_capture_path(
    label: Optional[str] = None,
    *,
    now: Optional[dt.datetime] = None,
    base: Optional[Path] = None,
) -> Path:
    """Return a fresh timestamped path under the captures directory.

    ``now`` and ``base`` are test hooks; production callers pass neither.
    The returned path's parent directory is created as a side effect.
    """
    directory = Path(base) if base is not None else CAPTURES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    suffix = f"-{_sanitize_label(label)}" if label else ""
    return directory / f"{stamp}{suffix}.bin"


def list_captures(base: Optional[Path] = None) -> list[Path]:
    directory = Path(base) if base is not None else CAPTURES_DIR
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.bin"))


class CaptureWriter:
    """Append-only raw-bytes sink for pty chunks. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # buffering=0 so a kill -9 mid-run still leaves a useful file.
        self._fh = self.path.open("ab", buffering=0)
        self._lock = threading.Lock()
        self._closed = False

    def write(self, chunk) -> None:
        if self._closed or not chunk:
            return
        if isinstance(chunk, str):
            data = chunk.encode("utf-8", errors="replace")
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            data = bytes(chunk)
        else:
            return
        with self._lock:
            if self._closed:
                return
            try:
                self._fh.write(data)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._fh.close()
            except Exception:
                pass

    def __enter__(self) -> "CaptureWriter":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
