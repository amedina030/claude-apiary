#!/usr/bin/env python3
"""
Structured run history for the runner.

The single append-only log of detached runs, one JSON object per line at
``<state>/runner/run_history.jsonl``. Superseded the flat overnight.jsonl.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .target_repo import run_history_path

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_HISTORY_FILE = run_history_path()

_DEFAULT_RUN_HISTORY_FILE = RUN_HISTORY_FILE


def _assert_isolated_in_test_mode(target: Path) -> None:
    """Refuse to write to the production run_history.jsonl when
    APIARY_RUNNER_TEST_ISOLATION=1 (T-2026-123).

    Tests in runner/test_run_detached.py drive run.run_detached() but
    historically left RUN_HISTORY_FILE pointing at the real production
    path, polluting it with fake 'test-uuid-1234' entries on every
    unit-test run. Same pattern as the budgeter T-3 guard: a mechanical
    invariant rather than a social contract.
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
    """Append one JSON line to run_history.jsonl. Returns True on success."""
    target = path or RUN_HISTORY_FILE
    _assert_isolated_in_test_mode(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        return False
    return True
