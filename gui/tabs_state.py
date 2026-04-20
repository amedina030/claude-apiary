"""Persist + restore the open-tabs list across GUI launches.

State file: ``~/.claude/apiary_gui/tabs.json``. Schema::

    {"tabs": ["D:/Professional/claude-apiary", "C:/Users/user/other-repo"],
     "active_idx": 0}

Missing / malformed / empty file -> returns default state (no tabs, idx=-1).
Non-existent cwds are silently dropped on load so a deleted project folder
can't block the GUI from starting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from gui.paths import state_dir


STATE_DIR = state_dir()
TABS_PATH = STATE_DIR / "tabs.json"


def load(path: Optional[Path] = None) -> tuple[list[Path], int]:
    """Read tabs state from disk. Returns (cwds, active_idx).

    - Missing/malformed file -> ([], -1).
    - Entries whose cwd is not an existing directory are filtered out.
    - active_idx is clamped into range after filtering.
    """
    p = path or TABS_PATH
    if not p.is_file():
        return [], -1
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], -1
    if not isinstance(data, dict):
        return [], -1
    raw_tabs = data.get("tabs", [])
    if not isinstance(raw_tabs, list):
        return [], -1
    cwds: list[Path] = []
    for entry in raw_tabs:
        if not isinstance(entry, str) or not entry:
            continue
        cand = Path(entry)
        if cand.is_dir():
            cwds.append(cand)
    if not cwds:
        return [], -1
    try:
        idx = int(data.get("active_idx", 0))
    except (TypeError, ValueError):
        idx = 0
    if idx < 0 or idx >= len(cwds):
        idx = 0
    return cwds, idx


def save(cwds: list[Path], active_idx: int, path: Optional[Path] = None) -> None:
    """Atomic-enough write: temp file + rename. Silent on error."""
    p = path or TABS_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tabs": [str(c) for c in cwds],
            "active_idx": int(active_idx),
        }
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass
