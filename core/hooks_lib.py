"""
Hook registration utilities for claude-apiary tools.

Used by setup.py to install/update hooks in Claude Code settings.json files
and by scripts/uninstall_hooks.py (todo #261) to take them back out again.
"""
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# Marker string that setup.py writes into absolute-path hook commands so
# they can be recognized on re-run. Historical marker — entries using
# the portable $CLAUDE_PROJECT_DIR template do NOT contain it, so
# callers must use is_apiary_entry() rather than a bare `MARKER in blob`
# check to avoid missing portable entries.
APIARY_MARKER = "claude-apiary"

# Path-suffix substrings that identify a hook entry as ours even when its
# command uses the portable $CLAUDE_PROJECT_DIR template (no absolute
# path containing APIARY_MARKER). Both forward- and back-slash variants
# are matched so hand-edited Windows entries are recognized too. (#227)
APIARY_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/budgeter/hooks/", "\\budgeter\\hooks\\",
    "/core/hooks/", "\\core\\hooks\\",
    "/scribe/", "\\scribe\\",
    "/docs/hooks/", "\\docs\\hooks\\",
    "/refiner/", "\\refiner\\",
    "/harden/", "\\harden\\",
    "/runner/", "\\runner\\",
)


def is_apiary_entry(entry: Any) -> bool:
    """Return True if a settings.json hook entry was installed by apiary.

    Recognizes two formats:
      1. Absolute-path entries written by ``setup.py --global`` whose
         path contains ``APIARY_MARKER`` ("claude-apiary").
      2. Portable hand-edited entries that use ``$CLAUDE_PROJECT_DIR/<sub>/...``
         and therefore lack the marker. Detected by known apiary subpath
         substrings (``/budgeter/hooks/``, ``/core/hooks/``, ``/scribe/``, …).

    Shared with setup.py's drift check and scripts/uninstall_hooks.py so
    the install and uninstall paths agree on which entries are ours.
    """
    blob = json.dumps(entry)
    if APIARY_MARKER in blob:
        return True
    return any(sub in blob for sub in APIARY_PATH_SUBSTRINGS)


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


def remove_hooks(settings_path: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """Remove every apiary-owned hook entry from ``settings_path``.

    The inverse of ``register_hooks``. Uses ``is_apiary_entry`` so
    portable ``$CLAUDE_PROJECT_DIR``-form entries are removed alongside
    absolute-path ones. Non-apiary entries are never touched.

    Empty event lists are pruned from the ``hooks`` dict so the resulting
    settings.json doesn't accumulate dead event keys. When every apiary
    entry was removed and no non-apiary entries remain, the top-level
    ``hooks`` key is deleted entirely.

    Returns a report dict::

        {
            "settings_path": "<path>",
            "existed": bool,              # False when the file was absent
            "removed": [                  # per-entry detail
                {"event": "PreToolUse", "entry": {...}},
                ...
            ],
            "remaining_counts": {"PreToolUse": 2, ...},  # post-uninstall
            "dry_run": dry_run,
        }

    With ``dry_run=True`` the file is not rewritten and the report still
    describes what would have been removed. Callers (``scripts/uninstall_hooks.py``)
    print the report for the operator.
    """
    report: Dict[str, Any] = {
        "settings_path": str(settings_path),
        "existed": settings_path.exists(),
        "removed": [],
        "remaining_counts": {},
        "dry_run": dry_run,
    }
    if not report["existed"]:
        return report

    settings = load_settings(settings_path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return report

    new_hooks: Dict[str, List] = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            # Preserve unexpected shapes verbatim — we don't own them.
            new_hooks[event] = entries
            continue
        kept: List = []
        for entry in entries:
            if is_apiary_entry(entry):
                report["removed"].append({"event": event, "entry": entry})
            else:
                kept.append(entry)
        if kept:
            new_hooks[event] = kept
        # else: drop empty event list

    report["remaining_counts"] = {
        event: len(entries) if isinstance(entries, list) else 1
        for event, entries in new_hooks.items()
    }

    if dry_run or not report["removed"]:
        return report

    if new_hooks:
        settings["hooks"] = new_hooks
    else:
        settings.pop("hooks", None)
    save_settings(settings_path, settings)
    return report
