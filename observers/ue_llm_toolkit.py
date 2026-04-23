#!/usr/bin/env python3
"""Reference observer for the ``ue-llm-toolkit`` MCP server — reader-call class.

Matches ``mcp__ue_llm_toolkit__*`` PostToolUse events. Writes one scribe
reference note per call summarising what was inspected. Shipped in the
apiary repo but only fires in target repos where the ``ue-llm-toolkit``
MCP server is loaded (Unreal projects).
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observer import ObservationEvent, run_observer

OBSERVER_NAME = "ue_llm_toolkit"
_TOOL_PREFIX = "mcp__ue_llm_toolkit__"
_LAUNCHER_PATH = Path.home() / ".claude" / "apiary_launch.py"
_SCRIBE_TIMEOUT_SECONDS = 15
_INPUT_SUMMARY_KEYS = ("path", "target", "name", "blueprint", "actor", "asset")


def handle(event: ObservationEvent) -> None:
    if not event.tool_name.startswith(_TOOL_PREFIX):
        return
    tool_input = event.raw.tool_input or {}
    summary = f"{event.tool_name}: {_summarise_input(tool_input)}"
    content = _build_content(event)
    _write_scribe_reference(summary, content)


def _summarise_input(tool_input: dict) -> str:
    for key in _INPUT_SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{key}={value[:80]}"
    keys = [k for k in tool_input.keys() if not k.startswith("_")]
    if keys:
        return "keys=[" + ", ".join(keys[:3]) + "]"
    return "(no input)"


def _build_content(event: ObservationEvent) -> str:
    try:
        input_repr = json.dumps(event.raw.tool_input, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        input_repr = "<unserialisable>"
    return (
        f"**Observer:** {OBSERVER_NAME}\n"
        f"**Tool:** `{event.tool_name}`\n"
        f"**Session:** `{event.session_id}`\n"
        f"**tool_use_id:** `{event.tool_use_id}`\n\n"
        f"**Input:**\n```json\n{input_repr}\n```\n"
    )


def _write_scribe_reference(summary: str, content: str) -> None:
    if not _LAUNCHER_PATH.is_file():
        return
    subprocess.run(
        [
            sys.executable,
            str(_LAUNCHER_PATH),
            "scribe/notes.py",
            "add",
            "--type",
            "reference",
            "--summary",
            summary,
            "--content",
            content,
            "--auto",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_SCRIBE_TIMEOUT_SECONDS,
    )


if __name__ == "__main__":
    sys.exit(run_observer(OBSERVER_NAME, handle))
