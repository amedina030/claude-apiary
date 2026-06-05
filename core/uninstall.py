"""``apiary uninstall --target <repo>`` — remove apiary from a bootstrapped repo.

Inverse of ``core/install.py``. See MIGRATION-PLAN.md §7.11.

Steps:

1. Hold registry FileLock.
2. Remove ``<repo>/.claude/apiary/`` (entire dir).
3. Remove apiary-copied slash commands from ``<repo>/.claude/commands/``,
   identified via ``bootstrap_state.json.commands_dir_hashes``.
4. Strip apiary-managed hook entries from ``<repo>/.claude/settings.json``
   (uses ``hooks_lib.remove_hooks``).
5. Strip the apiary-managed zone from ``<repo>/CLAUDE.md`` (preserve
   surrounding user content).
6. Remove the registry entry.
7. Optionally remove ``<main-apiary>/.repos/<slug>/`` (per-target state).
   The default keeps the data; pass ``remove_data=True`` to delete.

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

    # Resolve via self-pointer first (most accurate), fall back to registry
    # path-lookup so we still work after a partial install where the
    # self-pointer was never written.
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
        registry_entry_removed = uid is not None and str(uid) in registry
        if registry_entry_removed:
            del registry[str(uid)]
            state._save_registry(apiary, registry)

    # 2. Per-repo .claude/apiary/ dir
    pin = state.pin_dir(target_root)
    pin_removed = False
    if pin.is_dir():
        shutil.rmtree(pin)
        pin_removed = True

    # 3. Slash commands we installed
    commands_removed = _remove_apiary_commands(target_root, state_dir)

    # 4. Hook entries from settings.json
    settings_path = target_root / ".claude" / "settings.json"
    report = remove_hooks(settings_path)
    hook_entries_removed = len(report.get("removed", []))

    # 5. CLAUDE.md zone
    claude_md_zone_removed = _strip_claude_md_zone(target_root)

    # 7. Per-target state dir (centralized)
    state_dir_removed = False
    if remove_data and state_dir is not None and state_dir.is_dir():
        shutil.rmtree(state_dir)
        state_dir_removed = True

    return UninstallResult(
        uid=uid, name=name,
        target_repo=target_root, apiary_repo=apiary,
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
