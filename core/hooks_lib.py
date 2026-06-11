"""
Hook registration utilities for claude-apiary tools.

Used by setup.py to install/update hooks in Claude Code settings.json files
and by scripts/uninstall_hooks.py (todo #261) to take them back out again.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# Env override for the Python interpreter baked into generated commands.
# No single bare name (`python` / `python3` / `py`) is portable across
# macOS, Linux, and Windows, so callers never hardcode one — they resolve
# through resolve_python(), and a user with a non-standard setup can point
# every layer at one interpreter by exporting this single variable.
APIARY_PYTHON_ENV = "APIARY_PYTHON"


def resolve_python() -> Path:
    """Return the Python 3 interpreter to bake into generated hook commands.

    Honors the ``APIARY_PYTHON`` env override (an absolute path or a bare
    command name on PATH); otherwise falls back to the interpreter currently
    running this code (``sys.executable``) — which is by definition a working
    Python 3, since apiary can't run without one. Centralized so every layer
    that emits a Python invocation (``hooks_factory``, the bash git hooks via
    the same env var) resolves identically and one override reaches them all.
    """
    override = os.environ.get(APIARY_PYTHON_ENV, "").strip()
    if override:
        return Path(override)
    return Path(sys.executable)

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

    Recognizes four formats:
      1. Absolute-path entries written by ``setup.py --global`` whose
         path contains ``APIARY_MARKER`` ("claude-apiary").
      2. Portable hand-edited entries that use ``$CLAUDE_PROJECT_DIR/<sub>/...``
         and therefore lack the marker. Detected by known apiary subpath
         substrings (``/budgeter/hooks/``, ``/core/hooks/``, ``/scribe/``, …).
      3. Launcher-based entries that use ``apiary_launch.py`` to locate
         the apiary repo via the pointer file.
      4. Per-repo launcher entries (post-migration) whose commands invoke
         ``$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py``. Distinct from
         the global ``apiary_launch.py`` — both substrings are checked.

    Shared with setup.py's drift check and scripts/uninstall_hooks.py so
    the install and uninstall paths agree on which entries are ours.
    """
    blob = json.dumps(entry)
    if APIARY_MARKER in blob:
        return True
    if "apiary_launch.py" in blob:
        return True
    if ".claude/apiary/launch.py" in blob or ".claude\\\\apiary\\\\launch.py" in blob:
        return True
    return any(sub in blob for sub in APIARY_PATH_SUBSTRINGS)


def to_bash_path(p: Path) -> str:
    """Convert a Windows path to bash-compatible form (e.g. D:/foo -> /d/foo)."""
    s = p.as_posix()
    return re.sub(r'^([A-Za-z]):', lambda m: '/' + m.group(1).lower(), s)


def hook_cmd(
    script_path: Path,
    python_exe: Path = None,
    *,
    repo_root: Path = None,
    per_repo_launcher: bool = False,
) -> str:
    """Build a hook command string using bash-compatible paths.

    Three modes, selected by the keyword args:

    - ``per_repo_launcher=True`` (requires ``repo_root``): emits
      ``python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" <rel>``.
      The launcher is the per-repo shim written by ``apiary install``;
      Claude Code expands ``$CLAUDE_PROJECT_DIR`` at hook-fire time.
      Used by phase-1+ installs.
    - ``repo_root`` only: emits ``python ~/.claude/apiary_launch.py <rel>``.
      The historical global launcher mode (used by ``setup.py --global``).
    - Neither: legacy absolute-path format suitable for ``--project-path``
      installs where the session cwd matches the hook repo.

    *repo_root* in the launcher modes is **main-apiary's** root, not the
    bootstrapped repo's — the path is made relative so the launcher can
    re-resolve it against main-apiary at runtime.
    """
    if repo_root is not None:
        rel = script_path.relative_to(repo_root).as_posix()
        # Embed the resolved interpreter (bash-converted absolute path) rather
        # than a bare `python`. `python` is absent on a stock macOS Homebrew box
        # (only `python3` exists) and `python3` is absent on a stock Windows box
        # (only `python`), so no single bare command is portable. settings.json
        # is regenerated per-machine by `apiary install`, so the absolute path is
        # always valid on the machine that wrote it. The script path stays
        # portable via $CLAUDE_PROJECT_DIR / $HOME.
        exe = to_bash_path(python_exe or resolve_python())
        if per_repo_launcher:
            return f'"{exe}" "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" {rel}'
        # $HOME (double-quoted) rather than a bare ~ so a home dir with a space
        # or apostrophe (e.g. C:\Users\Nelson's PC) survives shell word-splitting.
        # A bare ~ cannot be quoted without suppressing its expansion.
        return f'"{exe}" "$HOME/.claude/apiary_launch.py" {rel}'
    if per_repo_launcher:
        raise ValueError("per_repo_launcher=True requires repo_root")
    exe = python_exe or resolve_python()
    # Quote both paths — the interpreter or script can live under a home dir
    # with a space/apostrophe, which would otherwise break the unquoted command.
    return f'"{to_bash_path(exe)}" "{to_bash_path(script_path)}"'


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
    marker (or any string in also_strip) in their JSON representation,
    or that ``is_apiary_entry()`` recognizes as ours.

    new_hooks: {event_name: [hook_entry, ...]}
    marker: string identifying this tool's hooks (e.g. "claude-apiary")
    also_strip: additional marker strings to remove (e.g. old repo paths)
    """
    strip = [marker] + (also_strip or [])
    settings = load_settings(settings_path)
    merged = settings.get("hooks", {})

    for event, entries in new_hooks.items():
        existing = merged.get(event, [])
        cleaned = [
            h for h in existing
            if not any(m in json.dumps(h) for m in strip)
            and not is_apiary_entry(h)
        ]
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
