"""Persisted composer-input height (px), set by dragging the gutter above
the chat input.

State file: ``<state_dir>/composer_state.json`` -- per-profile, same
state_dir() that sidebar_state and tabs_state use. Schema::

    { "height_px": 120 }

A height of 0 (or a missing file) means "use the CSS default" -- the
frontend skips the explicit style override in that case.
"""

from __future__ import annotations

import json
from pathlib import Path

from gui.paths import state_dir


STATE_PATH = state_dir() / "composer_state.json"
DEFAULT_HEIGHT = 0
MIN_HEIGHT = 24
MAX_HEIGHT = 4000


def load(path: Path = STATE_PATH) -> int:
    if not path.is_file():
        return DEFAULT_HEIGHT
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_HEIGHT
    if not isinstance(data, dict):
        return DEFAULT_HEIGHT
    h = data.get("height_px", DEFAULT_HEIGHT)
    if isinstance(h, (int, float)) and MIN_HEIGHT <= h <= MAX_HEIGHT:
        return int(h)
    return DEFAULT_HEIGHT


def save(height_px: int, path: Path = STATE_PATH) -> bool:
    if not isinstance(height_px, (int, float)):
        return False
    h = int(height_px)
    if not (MIN_HEIGHT <= h <= MAX_HEIGHT):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"height_px": h}, indent=2),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False
