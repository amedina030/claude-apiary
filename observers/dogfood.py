#!/usr/bin/env python3
"""Dogfood observer — proves the Phase 1 pipeline fires live in apiary sessions.

Matches ``Grep`` PostToolUse events so we can verify the adapter + failure-path
contract end to end inside the apiary repo itself (the reference
``ue_llm_toolkit`` observer only fires in Unreal projects where that MCP
server is loaded).

Returns a one-line summary dict per Grep call; the harness lands the full
event + summary as a JSONL entry under ``.apiary/observer/<date>/<session>.jsonl``.
No scribe write. To silence the dogfood once Phase 1 is validated, remove
its matcher from ``.claude/settings.json``.
"""
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observer import ObservationEvent, run_observer

OBSERVER_NAME = "dogfood"
_EXPECTED_TOOL = "Grep"


def handle(event: ObservationEvent) -> Optional[dict]:
    if event.tool_name != _EXPECTED_TOOL:
        return None
    tool_input = event.raw.tool_input or {}
    pattern = _short(tool_input.get("pattern", ""))
    search_path = _short(tool_input.get("path", "") or tool_input.get("glob", ""))
    return {
        "pattern": pattern,
        "path": search_path,
    }


def _short(value: object, limit: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


if __name__ == "__main__":
    sys.exit(run_observer(OBSERVER_NAME, handle))
