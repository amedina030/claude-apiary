"""Persisted sidebar UI state — currently just the collapsed/expanded state per group.

State file: `~/.claude/apiary_gui/sidebar_state.json`. Schema:

    { "collapsed": ["todo-deferred", "handoff", ...] }

The frontend is the source of truth in-memory; we just persist whenever the user
toggles a group, so a restart preserves their view.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from gui.paths import state_dir


STATE_PATH = state_dir() / "sidebar_state.json"


def load(path: Path = STATE_PATH) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("collapsed", [])
    if not isinstance(items, list):
        return []
    return [str(x) for x in items if isinstance(x, str)]


def save(collapsed: Iterable[str], path: Path = STATE_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"collapsed": sorted(set(collapsed))}, indent=2),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False
