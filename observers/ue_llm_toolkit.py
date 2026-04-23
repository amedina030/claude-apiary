#!/usr/bin/env python3
"""Reference observer for the ``ue-llm-toolkit`` MCP server — reader-call class.

Matches ``mcp__ue_llm_toolkit__*`` PostToolUse events. Returns a summary
dict per call; the harness lands the full event + summary as a JSONL
entry under ``.apiary/observer/<date>/<session>.jsonl``. Shipped in the
apiary repo but only fires in target repos where the ``ue-llm-toolkit``
MCP server is loaded (Unreal projects).
"""
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observer import ObservationEvent, run_observer

OBSERVER_NAME = "ue_llm_toolkit"
_TOOL_PREFIX = "mcp__ue_llm_toolkit__"
_INPUT_SUMMARY_KEYS = ("path", "target", "name", "blueprint", "actor", "asset")


def handle(event: ObservationEvent) -> Optional[dict]:
    if not event.tool_name.startswith(_TOOL_PREFIX):
        return None
    tool_input = event.raw.tool_input or {}
    return {
        "tool": event.tool_name,
        "input_summary": _summarise_input(tool_input),
    }


def _summarise_input(tool_input: dict) -> str:
    for key in _INPUT_SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{key}={value[:80]}"
    keys = [k for k in tool_input.keys() if not k.startswith("_")]
    if keys:
        return "keys=[" + ", ".join(keys[:3]) + "]"
    return "(no input)"


if __name__ == "__main__":
    sys.exit(run_observer(OBSERVER_NAME, handle))
