#!/usr/bin/env python3
"""Dogfood observer — proves the Phase 1 pipeline fires live in apiary sessions.

Matches ``Grep`` PostToolUse events so we can verify the adapter + failure-path
contract end to end inside the apiary repo itself (the reference
``ue_llm_toolkit`` observer only fires in Unreal projects where that MCP
server is loaded).

Writes one scribe reference note per fire summarising the pattern + path
searched. To avoid noise once Phase 1 is validated, either remove this
observer's matcher from ``.claude/settings.json`` or replace it with an
observer scoped to a tool the user cares about recording.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observer import ObservationEvent, run_observer

OBSERVER_NAME = "dogfood"
_EXPECTED_TOOL = "Grep"
_LAUNCHER_PATH = Path.home() / ".claude" / "apiary_launch.py"
_SCRIBE_TIMEOUT_SECONDS = 15


def handle(event: ObservationEvent) -> None:
    if event.tool_name != _EXPECTED_TOOL:
        return
    tool_input = event.raw.tool_input or {}
    pattern = _short(tool_input.get("pattern", ""))
    search_path = _short(tool_input.get("path", "") or tool_input.get("glob", ""))
    summary = f"Grep: {pattern}"
    if search_path:
        summary = f"{summary} in {search_path}"
    content = (
        f"Dogfood observer caught a live Grep call.\n\n"
        f"- pattern: `{pattern}`\n"
        f"- path/glob: `{search_path}`\n"
        f"- session: `{event.session_id}`\n"
        f"- tool_use_id: `{event.tool_use_id}`\n"
    )
    _write_scribe_reference(summary, content)


def _short(value: object, limit: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


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
