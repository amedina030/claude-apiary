"""theme.json hot-reload via watchdog (250ms debounce).

theme.json contains a flat object of CSS variable values:

    {"--bg": "#0e1116", "--accent": "#58a6ff", ...}

Keys may be passed without the leading `--`; the frontend normalizes them.
On malformed JSON the frontend keeps current styles and a non-blocking toast
fires; the watcher keeps running so a subsequent fix is picked up live.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from gui.paths import state_dir


THEME_DIR = state_dir()
THEME_PATH = THEME_DIR / "theme.json"
LAUNCH_PATH = THEME_DIR / "launch.json"

DEFAULT_THEME = {
    "--bg": "#0e1116",
    "--fg": "#e6edf3",
    "--muted": "#8b949e",
    "--accent": "#58a6ff",
    "--user-msg-bg": "#1f2937",
    "--user-msg-fg": "#e6edf3",
    "--asst-msg-bg": "#161b22",
    "--asst-msg-fg": "#e6edf3",
    "--header-bg": "#0d1117",
    "--sidebar-bg": "#0d1117",
    "--pty-bg": "#010409",
    "--pty-fg": "#c9d1d9",
    "--border": "#30363d",
    "--badge-bg": "#21262d",
    "--badge-fg": "#c9d1d9",
    "--font-size": "14px",
    "--radius": "6px",
}

DEFAULT_LAUNCH: dict = {
    "command": "claude",
    "args": [],
    "cwd": "",
    "rows": 40,
    "cols": 120,
}


def ensure_defaults() -> None:
    """Create theme.json + launch.json with defaults if missing."""
    THEME_DIR.mkdir(parents=True, exist_ok=True)
    if not THEME_PATH.is_file():
        THEME_PATH.write_text(json.dumps(DEFAULT_THEME, indent=2), encoding="utf-8")
    if not LAUNCH_PATH.is_file():
        LAUNCH_PATH.write_text(json.dumps(DEFAULT_LAUNCH, indent=2), encoding="utf-8")


def load_theme(path: Path = THEME_PATH) -> tuple[dict, Optional[str]]:
    """Read theme.json. Returns (vars, error). On error, returns DEFAULT_THEME and the error."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except FileNotFoundError:
        return dict(DEFAULT_THEME), None
    except (OSError, json.JSONDecodeError) as e:
        return dict(DEFAULT_THEME), f"theme.json invalid: {e}"
    if not isinstance(data, dict):
        return dict(DEFAULT_THEME), "theme.json must be a JSON object"
    # Per-key fallback: missing keys take the default for just that key.
    merged = dict(DEFAULT_THEME)
    for k, v in data.items():
        if isinstance(v, (str, int, float)):
            merged[k] = str(v)
    return merged, None


def load_launch(path: Path = LAUNCH_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_LAUNCH)
    if not isinstance(data, dict):
        return dict(DEFAULT_LAUNCH)
    out = dict(DEFAULT_LAUNCH)
    out.update({k: v for k, v in data.items() if k in DEFAULT_LAUNCH})
    return out


class ThemeWatcher:
    """Watch theme.json with watchdog; fire on_reload(vars, err) after a 250ms debounce.

    A debounce window is needed because some editors (VS Code, vim with backup)
    perform an "atomic save" — write to a tempfile, fsync, then rename — which
    fires multiple watchdog events for one logical save.
    """

    def __init__(
        self,
        on_reload: Callable[[dict, Optional[str]], None],
        path: Path = THEME_PATH,
        debounce: float = 0.25,
    ) -> None:
        self._on_reload = on_reload
        self._path = path
        self._debounce = debounce
        self._observer = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _fire(self) -> None:
        vars_, err = load_theme(self._path)
        try:
            self._on_reload(vars_, err)
        except Exception:
            pass

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def start(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return

        target_dir = self._path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = self._path.name

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if not event.is_directory and Path(event.src_path).name == target_name:
                    watcher._schedule()

            def on_created(self, event):
                if not event.is_directory and Path(event.src_path).name == target_name:
                    watcher._schedule()

            def on_moved(self, event):
                # Atomic-save flow: editor writes a temp file then renames it
                # over the target. The "moved" event's dest_path is the real save.
                dest = getattr(event, "dest_path", "")
                if dest and Path(dest).name == target_name:
                    watcher._schedule()

        observer = Observer()
        observer.schedule(Handler(), str(target_dir), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        # Push initial state synchronously so the UI styles up front.
        self._fire()

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
