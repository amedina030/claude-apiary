#!/usr/bin/env python3
"""
Structured run history for the runner.

Replaces the flat overnight.jsonl with a more structured append-only log.
Also writes to overnight.jsonl for backward compatibility.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .detached_lib import append_overnight_log

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_HISTORY_FILE = SCRIPT_DIR / "run_history.jsonl"

_DEFAULT_RUN_HISTORY_FILE = RUN_HISTORY_FILE


def _assert_isolated_in_test_mode(target: Path) -> None:
    """Refuse to write to the production run_history.jsonl when
    APIARY_RUNNER_TEST_ISOLATION=1 (T-2026-123).

    Tests in runner/test_run_detached.py drive run.run_detached() with a
    tempdir OVERNIGHT_LOG but historically left RUN_HISTORY_FILE pointing
    at the real production path, polluting it with fake 'test-uuid-1234'
    entries on every unit-test run. Same pattern as the budgeter T-3
    guard: a mechanical invariant rather than a social contract.
    """
    if os.environ.get("APIARY_RUNNER_TEST_ISOLATION") != "1":
        return
    if Path(target).resolve() == Path(_DEFAULT_RUN_HISTORY_FILE).resolve():
        raise RuntimeError(
            f"runner test-isolation violation: write to default run history "
            f"path {_DEFAULT_RUN_HISTORY_FILE} while "
            f"APIARY_RUNNER_TEST_ISOLATION=1. Patch "
            f"runner.run_history.RUN_HISTORY_FILE to a tempdir path in "
            f"setUp, or pass path= explicitly."
        )


def append_entry(entry: dict, path: Optional[Path] = None) -> bool:
    """Append one JSON line to run_history.jsonl.

    Also writes to overnight.jsonl for backward compatibility.
    Returns True on success.
    """
    target = path or RUN_HISTORY_FILE
    _assert_isolated_in_test_mode(target)
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
