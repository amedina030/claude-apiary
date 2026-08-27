"""Persist + restore the open-tabs list across GUI launches.

State file: ``~/.claude/apiary_gui/tabs.json``. Schema::

    {"tabs": [
        {"cwd": "D:/Professional/claude-apiary",
         "accept_edits": false},
        ...
     ],
     "active_idx": 0}

Legacy string-only entries (``"tabs": ["D:/...", "C:/..."]``) are still
accepted on load and upgraded to the object form with default settings.

Missing / malformed / empty file -> returns default state (no tabs, idx=-1).
Non-existent cwds are silently dropped on load so a deleted project folder
can't block the GUI from starting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gui.paths import state_dir


STATE_DIR = state_dir()
TABS_PATH = STATE_DIR / "tabs.json"


@dataclass
class TabEntry:
    cwd: Path
    accept_edits: bool = False


def load(path: Optional[Path] = None) -> tuple[list[TabEntry], int]:
    """Read tabs state from disk. Returns (entries, active_idx).

    - Missing/malformed file -> ([], -1).
    - Entries whose cwd is not an existing directory are filtered out.
    - active_idx is clamped into range after filtering.
    - Legacy string entries upgrade to TabEntry with default settings.
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
    entries: list[TabEntry] = []
    for raw in raw_tabs:
        if isinstance(raw, str) and raw:
            cand = Path(raw)
            if cand.is_dir():
                entries.append(TabEntry(cwd=cand))
            continue
        if isinstance(raw, dict):
            cwd_str = raw.get("cwd")
            if not isinstance(cwd_str, str) or not cwd_str:
                continue
            cand = Path(cwd_str)
            if not cand.is_dir():
                continue
            entries.append(TabEntry(
                cwd=cand,
                accept_edits=bool(raw.get("accept_edits", False)),
            ))
    if not entries:
        return [], -1
    try:
        idx = int(data.get("active_idx", 0))
    except (TypeError, ValueError):
        idx = 0
    if idx < 0 or idx >= len(entries):
        idx = 0
    return entries, idx


def save(entries: list[TabEntry], active_idx: int, path: Optional[Path] = None) -> None:
    """Atomic-enough write: temp file + rename. Silent on error."""
    p = path or TABS_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tabs": [
                {
                    "cwd": str(e.cwd),
                    "accept_edits": bool(e.accept_edits),
                }
                for e in entries
            ],
            "active_idx": int(active_idx),
        }
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass
