"""``apiary uninstall --target <repo>`` — remove apiary from a bootstrapped repo.

Inverse of ``core/install.py``. See ``docs/architecture/per-repo-install.md``.

Steps, in order — **files first, registry last**:

1. Refuse outright when the target is main-apiary itself.
2. Under the registry FileLock: look up the uid/name/state dir. Read-only.
3. Remove ``<repo>/.claude/apiary/`` (entire dir).
4. Remove apiary-copied slash commands from ``<repo>/.claude/commands/``,
   identified via ``bootstrap_state.json.commands_dir_hashes``.
5. Strip apiary-managed hook entries from ``<repo>/.claude/settings.json``
   (uses ``hooks_lib.remove_hooks``).
6. Strip the apiary-managed zone from ``<repo>/CLAUDE.md`` (preserve
   surrounding user content).
7. Under the registry FileLock again: remove the registry entry.
8. Optionally remove ``<main-apiary>/.repos/<slug>/`` (per-target state).
   The default keeps the data; pass ``remove_data=True`` to delete.

The ordering is the whole safety property. Deleting the registry entry first
meant any failure in steps 3-6 — a ``PermissionError`` on ``launch.py`` while a
hook process still holds it, a tampered CLAUDE.md — left a repo carrying pin
files, hooks and commands that no registry entry accounted for: the next
session then sees a self-pointer uid the registry does not know, and every
tool's state silently reroutes. Failing with the entry still in place instead
leaves the repo re-uninstallable.

Returns :class:`UninstallResult` summarizing what was touched.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import context_rules as cr
from core.hooks_lib import remove_hooks
from core.utils import state
from core.utils.filelock import FileLock


class UninstallError(Exception):
    """Raised when uninstall can't safely proceed."""


@dataclasses.dataclass
class UninstallResult:
    """Summary of an uninstall run."""

    uid: int | None
    name: str | None
    target_repo: Path
    apiary_repo: Path
    pin_dir_removed: bool
    commands_removed: list[str]
    hook_entries_removed: int
    claude_md_zone_removed: bool
    registry_entry_removed: bool
    state_dir_removed: bool


def uninstall(
    target_repo: Path,
    *,
    apiary_repo: Path | None = None,
    remove_data: bool = False,
) -> UninstallResult:
    """Reverse a previous ``install`` for *target_repo*.

    Set ``remove_data=True`` to also delete the centralized per-target
    state at ``<main-apiary>/.repos/<slug>/``. Default is to keep it
    (the ``--keep-data`` variant of the CLI).
    """
    target_root = Path(target_repo).resolve()
    if not target_root.is_dir():
        raise UninstallError(f"target {target_root} is not a directory")
    apiary = state.resolve_apiary_repo(apiary_repo).resolve()

    # 1. Never uninstall the toolkit out from under itself. main-apiary's
    # .claude/apiary/ and registry entry are what every other bootstrapped
    # repo resolves through, and `--remove-data` would delete apiary's own
    # scribe notes as a bonus. `git_hooks.install` guards the same case.
    if target_root == apiary:
        raise UninstallError(
            f"refusing to uninstall main-apiary itself ({target_root}). Every "
            "bootstrapped repo resolves through this checkout's registry and "
            "pin files. To retire it, uninstall the repos it manages first "
            "(`apiary doctor registry` lists them), then delete the clone."
        )

    # 2. Resolve via self-pointer first (most accurate), fall back to registry
    # path-lookup so we still work after a partial install where the
    # self-pointer was never written. Read-only: the entry is deleted at
    # step 7, after every file step has succeeded.
    self_p = state.read_self_pointer(target_root)
    uid: int | None = None
    name: str | None = None
    state_dir: Path | None = None

    with FileLock(state.registry_path(apiary)):
        registry = state._load_registry(apiary)
        if self_p is not None:
            uid_str = str(self_p.get("uid", ""))
            if uid_str in registry:
                uid = self_p["uid"]
                name = registry[uid_str].get("name")
        if uid is None:
            match = state._find_entry_by_path(registry, target_root)
            if match is not None:
                uid_str, entry = match
                uid = int(uid_str)
                name = entry.get("name")
        if uid is not None and name is not None:
            state_dir = state.repos_dir(apiary) / f"{name}-{uid}"

    # 3. Per-repo .claude/apiary/ dir
    pin = state.pin_dir(target_root)
    pin_removed = False
    if pin.is_dir():
        shutil.rmtree(pin)
        pin_removed = True

    # 4. Slash commands we installed
    commands_removed = _remove_apiary_commands(target_root, state_dir)

    # 5. Hook entries from settings.json
    settings_path = target_root / ".claude" / "settings.json"
    report = remove_hooks(settings_path)
    hook_entries_removed = len(report.get("removed", []))

    # 6. CLAUDE.md zone
    claude_md_zone_removed = _strip_claude_md_zone(target_root)

    # 7. Registry entry — last, so a failure above leaves the repo registered
    # and re-uninstallable rather than half-removed and unaccounted for.
    registry_entry_removed = False
    if uid is not None:
        with FileLock(state.registry_path(apiary)):
            registry = state._load_registry(apiary)
            if str(uid) in registry:
                del registry[str(uid)]
                state._save_registry(apiary, registry)
                registry_entry_removed = True

    # 8. Per-target state dir (centralized)
    state_dir_removed = False
    if remove_data and state_dir is not None and state_dir.is_dir():
        shutil.rmtree(state_dir)
        state_dir_removed = True

    return UninstallResult(
        uid=uid,
        name=name,
        target_repo=target_root,
        apiary_repo=apiary,
        pin_dir_removed=pin_removed,
        commands_removed=commands_removed,
        hook_entries_removed=hook_entries_removed,
        claude_md_zone_removed=claude_md_zone_removed,
        registry_entry_removed=registry_entry_removed,
        state_dir_removed=state_dir_removed,
    )


def _remove_apiary_commands(target_root: Path, state_dir: Path | None) -> list[str]:
    """Delete slash command files we installed, identified via
    bootstrap_state.json.commands_dir_hashes when available, otherwise
    by name match against the source dirs."""
    cmds_dir = target_root / ".claude" / "commands"
    if not cmds_dir.is_dir():
        return []

    names_to_remove: set[str] = set()
    if state_dir is not None:
        bs_path = state_dir / "bootstrap_state.json"
        if bs_path.is_file():
            try:
                bs = json.loads(bs_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                bs = {}
            names_to_remove.update(bs.get("commands_dir_hashes", {}).keys())

    if not names_to_remove:
        # Fallback: match against the canonical install set. Slower but
        # works when bootstrap_state is missing (partial install).
        from core import install as install_mod

        # Resolve apiary via env or pointer — fall back to the parent of
        # state_dir if we can find it.
        apiary = state_dir.parent.parent if state_dir is not None else None
        if apiary is not None and apiary.is_dir():
            for src in install_mod._slash_command_sources(apiary):
                names_to_remove.add(src.name)

    removed: list[str] = []
    for name in sorted(names_to_remove):
        target = cmds_dir / name
        if target.is_file():
            target.unlink()
            removed.append(name)

    # If commands dir is now empty, remove it so .claude/ is tidy.
    try:
        if cmds_dir.is_dir() and not any(cmds_dir.iterdir()):
            cmds_dir.rmdir()
    except OSError:
        pass

    return removed


def _strip_claude_md_zone(target_root: Path) -> bool:
    """Remove the apiary-managed zone from <target>/CLAUDE.md, preserving
    everything outside the sentinels. Returns True if a zone was found and
    removed."""
    p = target_root / "CLAUDE.md"
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8")
    try:
        zone = cr.find_managed_zone(text)
    except cr.ZoneTamperError:
        # Tampered zone: leave alone — operator should resolve manually.
        return False
    if zone is None:
        return False
    new_text = text[: zone.start] + text[zone.end :]
    # Tidy: strip a single trailing blank line introduced by zone removal
    # if the surrounding content didn't already end on a blank line.
    new_text = new_text.rstrip("\n") + "\n" if new_text.strip() else ""
    p.write_text(new_text, encoding="utf-8")
    return True
