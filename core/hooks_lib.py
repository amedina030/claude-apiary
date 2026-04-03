"""
Hook registration utilities for claude-apiary tools.

Used by setup.py to install/update hooks in Claude Code settings.json files.
"""
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List


def to_bash_path(p: Path) -> str:
    """Convert a Windows path to bash-compatible form (e.g. D:/foo -> /d/foo)."""
    s = p.as_posix()
    return re.sub(r'^([A-Za-z]):', lambda m: '/' + m.group(1).lower(), s)


def hook_cmd(script_path: Path, python_exe: Path = None) -> str:
    """Build a hook command string using bash-compatible paths."""
    exe = python_exe or Path(sys.executable)
    return f"{to_bash_path(exe)} {to_bash_path(script_path)}"


def load_settings(path: Path) -> Dict[str, Any]:
    """Load settings.json, returning empty dict if missing or invalid."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_settings(path: Path, settings: Dict[str, Any]) -> None:
    """Write settings.json, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def register_hooks(settings_path: Path, new_hooks: Dict[str, List], marker: str, also_strip: List[str] = None) -> None:
    """
    Merge new_hooks into settings_path, replacing any entries that contain
    marker (or any string in also_strip) in their JSON representation.

    new_hooks: {event_name: [hook_entry, ...]}
    marker: string identifying this tool's hooks (e.g. "claude-apiary")
    also_strip: additional marker strings to remove (e.g. old repo paths)
    """
    strip = [marker] + (also_strip or [])
    settings = load_settings(settings_path)
    merged = settings.get("hooks", {})

    for event, entries in new_hooks.items():
        existing = merged.get(event, [])
        cleaned = [h for h in existing if not any(m in json.dumps(h) for m in strip)]
        merged[event] = cleaned + entries

    settings["hooks"] = merged
    save_settings(settings_path, settings)
