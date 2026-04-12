#!/usr/bin/env python3
"""
Structured run history for the runner.

Replaces the flat overnight.jsonl with a more structured append-only log.
Also writes to overnight.jsonl for backward compatibility.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .detached_lib import append_overnight_log

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_HISTORY_FILE = SCRIPT_DIR / "run_history.jsonl"


def append_entry(entry: dict, path: Optional[Path] = None) -> bool:
    """Append one JSON line to run_history.jsonl.

    Also writes to overnight.jsonl for backward compatibility.
    Returns True on success.
    """
    target = path or RUN_HISTORY_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        return False

    # Backward compat: also log to overnight.jsonl
    if path is None:
        append_overnight_log(entry)

    return True


def read_entries(uuid: Optional[str] = None,
                 path: Optional[Path] = None) -> list[dict]:
    """Read all entries, optionally filtered by uuid. Newest last."""
    target = path or RUN_HISTORY_FILE
    if not target.exists():
        return []
    entries = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if uuid is None or entry.get("uuid") == uuid:
                entries.append(entry)
        except json.JSONDecodeError:
            continue
    return entries
