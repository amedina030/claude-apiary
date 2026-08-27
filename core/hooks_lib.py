"""
Hook registration utilities for claude-apiary tools.

Used by ``core/install.py`` to install/update hooks in Claude Code
settings.json files and by ``core/uninstall.py`` to take them back out.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from core.utils.atomic import write_json_atomic
from core.utils.jsonio import read_json_object

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

# The checkout name that appeared in the absolute paths the retired global
# installer wrote. Historical only, and NOT ownership on its own — see
# _LEGACY_HOOK_DIRS. Callers decide what is apiary's through is_apiary_entry().
APIARY_MARKER = "claude-apiary"

# The ownership mark every generated hook command now ends with. It rides in
# the command string rather than in a `"_apiary": true` field because a hook
# object is documented as {type, command, timeout, …} and nothing promises that
# unknown keys are preserved — a reader that drops them would take our only
# proof of ownership with them. As a trailing `#` comment it is inert in all
# three shells Claude Code uses for the shell form (`sh -c` on macOS/Linux,
# Git Bash on Windows, PowerShell when Git Bash is absent).
APIARY_HOOK_MARKER = " # claude-apiary"

# The mark as it appears inside a command string, without the separating space
# the shell needs — this is what entry matching looks for.
_MARKER_TOKEN = APIARY_HOOK_MARKER.strip()

# Launcher shapes written before the marker existed. Each names a file only an
# apiary install ever wrote, so they identify an entry on their own.
_LEGACY_LAUNCHERS: tuple[str, ...] = (
    "apiary_launch.py",              # retired global $HOME/.claude launcher
    ".claude/apiary/launch.py",      # per-repo launcher, pre-marker installs
    ".claude\\\\apiary\\\\launch.py",  # ditto, hand-edited Windows spelling
)

# The retired global installer wrote absolute paths into a `claude-apiary`
# checkout instead of a launcher. Recognizing those needs BOTH the checkout
# name and one of the hook directories below — a repo name on its own is not
# ownership. The old rule matched either half anywhere in the entry, which is
# how a user hook running `scripts/runner/lint.py` got deleted on every
# install (Bug 8); nothing here matches a command that lacks both.
_LEGACY_HOOK_DIRS: tuple[str, ...] = (
    "/budgeter/hooks/", "\\\\budgeter\\\\hooks\\\\",
    "/core/hooks/", "\\\\core\\\\hooks\\\\",
    "/docs/hooks/", "\\\\docs\\\\hooks\\\\",
    "/scribe/hooks/", "\\\\scribe\\\\hooks\\\\",
)


def is_apiary_entry(entry: Any) -> bool:
    """Return True if a settings.json hook entry was installed by apiary.

    Ownership is the explicit ``APIARY_HOOK_MARKER`` that ``hook_cmd`` appends
    to every command it generates, plus the two legacy shapes above so repos
    bootstrapped before the marker existed can still be cleaned up by a
    re-install or an uninstall. Nothing else counts: an entry the user wrote is
    never ours, however much its path resembles apiary's layout.

    Shared by the install and uninstall paths so they agree on which entries
    are ours.
    """
    blob = json.dumps(entry)
    if _MARKER_TOKEN in blob:
        return True
    if any(launcher in blob for launcher in _LEGACY_LAUNCHERS):
        return True
    return APIARY_MARKER in blob and any(d in blob for d in _LEGACY_HOOK_DIRS)


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
    args: tuple[str, ...] = (),
) -> str:
    """Build a hook command string using bash-compatible paths.

    Every command ends with ``APIARY_HOOK_MARKER`` — a shell comment that marks
    the entry as apiary's so ``is_apiary_entry`` can recognize it exactly,
    without guessing from path shapes.

    Two modes, selected by the keyword args:

    - ``per_repo_launcher=True`` (requires ``repo_root``): emits
      ``python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" <rel>``.
      The launcher is the per-repo shim written by ``apiary install``;
      Claude Code expands ``$CLAUDE_PROJECT_DIR`` at hook-fire time. This
      is the only mode ``hooks_factory`` uses.
    - Neither arg: legacy absolute-path format suitable for
      ``--project-path`` installs where the session cwd matches the hook
      repo.

    *repo_root* is **main-apiary's** root, not the bootstrapped repo's —
    the path is made relative so the launcher can re-resolve it against
    main-apiary at runtime.

    *args* are appended verbatim after the script (the dispatcher's event
    verb: ``... core/hooks/dispatch.py pre``). Restricted to a conservative
    slug so a command string can never be broken — or extended — by an
    argument.
    """
    for arg in args:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", arg or ""):
            raise ValueError(f"hook_cmd arg must be a plain token, got {arg!r}")
    suffix = ("".join(f" {a}" for a in args))
    if repo_root is not None:
        if not per_repo_launcher:
            # The third mode (`python "$HOME/.claude/apiary_launch.py" <rel>`)
            # belonged to the retired global install; that launcher no longer
            # exists on disk, so emitting a command for it would silently
            # install a broken hook.
            raise ValueError(
                "repo_root requires per_repo_launcher=True; the global "
                "~/.claude/apiary_launch.py mode was removed"
            )
        rel = script_path.relative_to(repo_root).as_posix()
        # Embed the resolved interpreter (bash-converted absolute path) rather
        # than a bare `python`. `python` is absent on a stock macOS Homebrew box
        # (only `python3` exists) and `python3` is absent on a stock Windows box
        # (only `python`), so no single bare command is portable. settings.json
        # is regenerated per-machine by `apiary install`, so the absolute path is
        # always valid on the machine that wrote it. The script path stays
        # portable via $CLAUDE_PROJECT_DIR.
        exe = to_bash_path(python_exe or resolve_python())
        return f'"{exe}" "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" {rel}{suffix}' + APIARY_HOOK_MARKER
    if per_repo_launcher:
        raise ValueError("per_repo_launcher=True requires repo_root")
    exe = python_exe or resolve_python()
    # Quote both paths — the interpreter or script can live under a home dir
    # with a space/apostrophe, which would otherwise break the unquoted command.
    return f'"{to_bash_path(exe)}" "{to_bash_path(script_path)}"{suffix}' + APIARY_HOOK_MARKER


def load_settings(path: Path) -> Dict[str, Any]:
    """Load settings.json, returning empty dict if missing or invalid."""
    return read_json_object(path) or {}


def save_settings(path: Path, settings: Dict[str, Any]) -> None:
    """Write settings.json, creating parent dirs as needed.

    Atomic: an interrupted install must not leave the user with a truncated
    settings.json — Claude Code would then start with no hooks at all
    (review X-3 flagged this as the one non-atomic writer left in core)."""
    write_json_atomic(path, settings, indent=2)


def register_hooks(settings_path: Path, new_hooks: Dict[str, List],
                   marker: str = APIARY_HOOK_MARKER, also_strip: List[str] = None) -> None:
    """
    Merge new_hooks into settings_path, replacing any entries that
    ``is_apiary_entry()`` recognizes as ours (or that contain *marker* / any
    string in *also_strip* in their JSON representation).

    new_hooks: {event_name: [hook_entry, ...]}
    marker: ownership mark on the entries being replaced. Defaults to
        ``APIARY_HOOK_MARKER``; do NOT pass a bare repo name here — a
        substring that broad matches user hooks too (Bug 8).
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
    describes what would have been removed. Callers print the report for
    the operator.
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
