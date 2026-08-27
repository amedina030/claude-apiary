"""``apiary install --target <repo>`` — per-repo bootstrap.

Generates the per-repo files under ``<repo>/.claude/apiary/`` (launcher,
pointers, version, flags/, session-tmp/), writes a ``settings.json`` whose
hooks dispatch through the per-repo launcher, copies slash commands, and
updates the registry. Idempotent — re-running refreshes generated files
without disturbing user-owned content.

See ``docs/architecture/per-repo-install.md`` for the full step list.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import context_rules as cr
from core import launcher_template
from core.hooks_lib import load_settings, register_hooks, save_settings
from core.utils import state
from core.utils.filelock import FileLock

# bootstrap_state.json schema — bumped to v2 by the per-repo migration to
# carry hash fields used by ``apiary doctor registry`` for drift detection.
_BOOTSTRAP_STATE_SCHEMA = 2


class InstallError(Exception):
    """Raised for user-facing install failures (bad target, profile error)."""


@dataclasses.dataclass
class InstallResult:
    """Summary returned by :func:`install`. Useful in tests and CLI output."""
    uid: int
    name: str
    slug: str
    target_repo: Path
    apiary_repo: Path
    state_dir: Path
    apiary_version: str
    is_first_install: bool


def install(
    target_repo: Path,
    *,
    profile: str = "base",
    apiary_repo: Path | None = None,
) -> InstallResult:
    """Idempotent per-repo install of apiary into *target_repo*.

    Steps (per §7.8):

    1. Resolve target's git root and main-apiary's path.
    2. Under registry FileLock: find/allocate uid, update registry entry,
       ensure per-target state dir exists.
    3. Generate per-repo files under ``<repo>/.claude/apiary/``.
    4. Write ``<repo>/.claude/settings.json`` with hooks pointing at the
       per-repo launcher.
    5. Copy slash commands into ``<repo>/.claude/commands/``.
    6. Write the apiary-managed zone into ``<repo>/CLAUDE.md``.
    7. Add ``.claude/`` to ``<repo>/.gitignore`` if not already present.
    8. Write ``bootstrap_state.json`` with hashes of generated files.

    Returns :class:`InstallResult` describing what was done.
    """
    target_root = _resolve_target(target_repo)
    apiary = state.resolve_apiary_repo(apiary_repo)
    apiary = apiary.resolve()

    uid, name, is_first, registered_at = _register_or_update(target_root, apiary)
    slug = f"{name}-{uid}"
    state_dir = state.repos_dir(apiary) / slug
    state_dir.mkdir(parents=True, exist_ok=True)
    _scaffold_scribe_templates(state_dir)

    apiary_version = state.read_apiary_version(apiary)
    now = state._now_iso()

    # Per-repo .claude/apiary/ files
    pin = state.pin_dir(target_root)
    pin.mkdir(parents=True, exist_ok=True)
    (pin / "flags").mkdir(exist_ok=True)
    (pin / "session-tmp").mkdir(exist_ok=True)
    _write_launcher(pin / "launch.py")
    state.write_main_apiary_pointer(target_root, {
        "main_apiary_path": str(apiary),
        "main_apiary_uid": 1,
        "registered_at": registered_at,
    })
    if state.read_self_pointer(target_root) is None:
        state.write_self_pointer(target_root, {
            "uid": uid,
            "name": name,
            "real_path": str(target_root),
            "registered_at": registered_at,
            "last_drift_check": now,
        })
    state.write_version(target_root, {
        "apiary_version": apiary_version,
        "pinned_at": now,
    })

    # Per-repo settings.json with hooks pointing at per-repo launcher
    settings_hash = _write_per_repo_settings(target_root, apiary, profile)

    # Slash commands
    commands_hashes = _copy_slash_commands(target_root, apiary, _previous_commands_hashes(state_dir))

    # CLAUDE.md managed zone
    claude_md_hash = _write_claude_md_zone(target_root, apiary)

    # .gitignore
    _ensure_gitignore_entry(target_root)

    # Commit-time secret scan. Installed here rather than only by the incubator
    # or the standalone script, because bootstrapping is the one moment every
    # managed repo passes through — a one-time sweep decays as soon as the next
    # repo is registered (#T-2026-261). Best-effort: a repo without the hook is
    # still a working repo, so a refusal warns rather than failing the install.
    _install_secret_scan_hook(target_root)

    # bootstrap_state.json with hashes for doctor's drift detection
    _write_bootstrap_state(state_dir, profile, apiary_version, settings_hash,
                           commands_hashes, claude_md_hash, is_first, now)

    return InstallResult(
        uid=uid, name=name, slug=slug,
        target_repo=target_root, apiary_repo=apiary,
        state_dir=state_dir, apiary_version=apiary_version,
        is_first_install=is_first,
    )


# --- Registry interaction -------------------------------------------------

def _resolve_target(target_repo: Path) -> Path:
    target = Path(target_repo).resolve()
    if not target.is_dir():
        raise InstallError(f"target {target} is not a directory")
    git_root = state._git_repo_root(target)
    if git_root is None:
        raise InstallError(
            f"target {target} is not inside a git repository — "
            "apiary requires a git repo to identify the target"
        )
    return git_root.resolve()


def _register_or_update(
    target_root: Path, apiary: Path,
) -> tuple[int, str, bool, str]:
    """Return (uid, name, is_first_install, registered_at_iso). Holds the
    registry FileLock for the read-update-write."""
    repos_root = state.repos_dir(apiary)
    repos_root.mkdir(parents=True, exist_ok=True)
    now = state._now_iso()
    apiary_version = state.read_apiary_version(apiary)

    with FileLock(state.registry_path(apiary)):
        registry = state._load_registry(apiary)
        match = state._find_entry_by_path(registry, target_root)
        if match is not None:
            uid_str, entry = match
            uid = int(uid_str)
            name = entry.get("name") or state._safe_name(target_root.name)
            registered_at = entry.get("registered_at", now)
            is_first = False
        else:
            uid = state.allocate_next_id(apiary)
            uid_str = str(uid)
            name = state._safe_name(target_root.name)
            registered_at = now
            is_first = True

        registry[uid_str] = {
            "name": name,
            "real_path": str(target_root),
            "uid": uid,
            "version": apiary_version,
            "registered_at": registered_at,
            "last_used": now,
            "verified_ok": True,
        }
        state._save_registry(apiary, registry)

    return uid, name, is_first, registered_at


# --- File generation ------------------------------------------------------

def _write_launcher(launcher_path: Path) -> None:
    """Write the per-repo launcher shim atomically. Marks executable on
    Unix-like systems (no-op on Windows)."""
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = launcher_path.with_name(launcher_path.name + ".tmp")
    tmp.write_text(launcher_template.LAUNCHER_PY, encoding="utf-8")
    tmp.replace(launcher_path)
    try:
        launcher_path.chmod(launcher_path.stat().st_mode | 0o755)
    except OSError:
        pass  # best-effort — not all filesystems support chmod


def _write_per_repo_settings(
    target_root: Path, apiary: Path, profile: str,
) -> str:
    """Generate ``<target>/.claude/settings.json`` with hooks dispatched
    through the per-repo launcher. Returns the sha256 of the written file.

    Uses ``core/hooks_factory``'s ``build_*_hooks`` so every install gets
    the same hook set, with the launcher path pointed at
    ``$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py``.
    """
    from core import hooks_factory
    from core.hooks_lib import hook_cmd

    hooks = {}
    for builder in (
        hooks_factory.build_core_hooks,
        hooks_factory.build_budgeter_hooks,
        hooks_factory.build_scribe_hooks,
        hooks_factory.build_docs_hooks,
    ):
        for event, entries in builder().items():
            hooks.setdefault(event, []).extend(entries)
    # Per-repo-only drift check — runs first on PreToolUse so the rest
    # of the chain sees an up-to-date self-pointer and registry entry.
    drift_cmd = hook_cmd(
        apiary / "core" / "hooks" / "per_repo_drift_check.py",
        repo_root=apiary, per_repo_launcher=True,
    )
    hooks.setdefault("PreToolUse", []).insert(0, {
        "matcher": "",
        "hooks": [{"type": "command", "command": drift_cmd}],
    })

    settings_path = target_root / ".claude" / "settings.json"
    register_hooks(
        settings_path,
        hooks,
        marker="claude-apiary",
    )

    # Apply per-repo profile permissions on top of hooks. The profile
    # system's _merge_profile_into_settings writes apiary-owned top-level
    # keys verbatim from the resolved profile; hooks are NOT in the
    # profile, so we register them above and the profile permissions
    # land alongside.
    _apply_profile_permissions(settings_path, apiary, profile)

    return _hash_file(settings_path)


def _apply_profile_permissions(settings_path: Path, apiary: Path, profile: str) -> None:
    """Merge the resolved profile's permissions into *settings_path*.

    No interactive drift prompt — the install contract is trusted
    (idempotent, profile is the source of truth for permissions).
    """
    from core.apiary_profiles import resolve

    resolved = resolve(profile, apiary / "profiles")
    settings = load_settings(settings_path)
    for key, value in resolved.merged.items():
        if key == "hooks":
            # Hooks are handled separately so per-repo launcher commands win.
            continue
        settings[key] = value
    save_settings(settings_path, settings)


def _copy_slash_commands(target_root: Path, apiary: Path,
                         previous_hashes: dict[str, str] | None = None) -> dict[str, str]:
    """Copy ``<apiary>/<tool>/commands/*.md`` into ``<target>/.claude/commands/``.
    Returns ``{filename: sha256}`` for the bootstrap_state hash record.

    A command apiary no longer ships is pruned from the target — but only
    when the installed copy still matches the hash recorded at the previous
    install (*previous_hashes*), i.e. the user never edited it. Without this
    every bootstrapped repo kept deleted commands forever, and the rewritten
    hash record then hid them from ``doctor stale`` and ``uninstall``.
    """
    dest = target_root / ".claude" / "commands"
    dest.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for src in _slash_command_sources(apiary):
        target = dest / src.name
        shutil.copy2(src, target)
        hashes[src.name] = _hash_file(target)
    for name, recorded in (previous_hashes or {}).items():
        if name in hashes:
            continue
        stale = dest / name
        if stale.is_file() and _hash_file(stale) == recorded:
            stale.unlink()
            print(f"  removed {stale} (no longer shipped by apiary)")
        elif stale.is_file():
            print(f"  kept {stale}: apiary no longer ships it but the copy was edited locally")
    return hashes


def _previous_commands_hashes(state_dir: Path) -> dict[str, str]:
    """The ``commands_dir_hashes`` recorded by the previous install, if any."""
    p = Path(state_dir) / "bootstrap_state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hashes = data.get("commands_dir_hashes") if isinstance(data, dict) else None
    return dict(hashes) if isinstance(hashes, dict) else {}


def _slash_command_sources(apiary: Path) -> Iterable[Path]:
    """Yield every ``<tool>/commands/*.md`` path under main-apiary."""
    for tool in (
        "budgeter", "scribe", "core", "docs", "refiner",
        "harden", "compass", "researcher", "runner", "incubator",
    ):
        cmd_dir = apiary / tool / "commands"
        if cmd_dir.is_dir():
            yield from sorted(cmd_dir.glob("*.md"))


def _write_claude_md_zone(target_root: Path, apiary: Path) -> str:
    """Write the apiary-managed zone into ``<target>/CLAUDE.md``.

    Reuses ``core/context_rules`` for rendering the sentinel-bounded
    zone. Existing user-owned content around the zone is preserved.

    Returns the sha256 of the rendered zone (NOT the whole file) for
    drift detection by ``apiary doctor registry``.
    """
    rules_dir = apiary / "context-rules"
    if not rules_dir.is_dir():
        return ""  # nothing to install — skip cleanly

    rules = cr.load_all_rules(rules_dir)
    rendered_zone = cr.render_managed_zone(rules)

    target = target_root / "CLAUDE.md"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    existing_zone = cr.find_managed_zone(existing) if existing else None

    if existing_zone is None:
        # Append the zone with a separating blank line if there was content.
        merged = (existing.rstrip() + "\n\n" if existing.strip() else "") + rendered_zone
    else:
        # Replace the bounded zone in place.
        merged = (
            existing[: existing_zone.start]
            + rendered_zone.rstrip("\n")
            + existing[existing_zone.end :]
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(merged, encoding="utf-8")
    return hashlib.sha256(rendered_zone.encode("utf-8")).hexdigest()


# Written into a freshly-bootstrapped repo's .gitignore. Excludes the same
# thing a bare `.claude/` would, but one level at a time, because git cannot
# re-include a file whose PARENT DIRECTORY is excluded. A repo that ships its
# own slash commands needs that seam: under a blanket `.claude/`, its command
# files are silently untracked — they work locally and vanish on clone, with
# no warning from git (#T-2026-258).
_GITIGNORE_BLOCK = """# Apiary-managed Claude Code install — per-machine, not tracked.
# Widened stepwise so this repo can re-include slash commands it owns:
# git can't un-ignore a file inside an ignored directory.
.claude/*
!.claude/commands/
.claude/commands/*
# To track a command this repo owns, list it here:
# !.claude/commands/<name>.md
"""

# Forms that already exclude .claude, in either the old blanket spelling or
# the stepwise one. Presence of any means we leave the file alone.
_GITIGNORE_PRESENT = (".claude/", "/.claude/", ".claude", "/.claude",
                      ".claude/*", "/.claude/*")

# The old spelling, which works but offers no way to track repo-owned commands.
_GITIGNORE_BLANKET = (".claude/", "/.claude/", ".claude", "/.claude")


def _scaffold_scribe_templates(state_dir: Path) -> None:
    """Seed ``<state-dir>/scribe/templates/`` with the bundled per-type templates.

    Bootstrap (and self-bootstrap, which routes through :func:`install`) is the
    one moment every managed repo passes through, so it is where the templates
    land. Existing templates are never overwritten — re-running install must
    not clobber a user's edits. Best-effort: a repo without templates still
    works (the gate simply doesn't apply), so a failure warns, never raises.
    """
    try:
        from scribe.notes import SCRIBE_SUBDIR, scaffold_default_templates
    except Exception as exc:  # noqa: BLE001 - never fail an install over this
        print(f"  scribe templates : SKIPPED (import failed: {exc})")
        return
    try:
        written = scaffold_default_templates(state_dir / SCRIBE_SUBDIR)
    except OSError as exc:
        print(f"  scribe templates : SKIPPED ({exc.__class__.__name__}: {exc})")
        return
    if written:
        print(f"  scribe templates : wrote {', '.join(written)}")
    else:
        print("  scribe templates : already present")


def _install_secret_scan_hook(target_root: Path) -> None:
    """Install the secret-scan pre-commit hook, reporting either way.

    Silence would be the wrong failure mode: a hook the operator believes is
    installed but isn't is worse than none at all, which is the whole reason
    this moved out of a one-off script.
    """
    try:
        from core import git_hooks
    except Exception as exc:  # noqa: BLE001 - never fail an install over this
        print(f"  secret-scan hook : SKIPPED (import failed: {exc})")
        return
    if git_hooks.is_main_apiary(target_root):
        # main-apiary runs the combined doc-check + secret-scan hook instead.
        print("  secret-scan hook : skipped (main-apiary uses install_repo_hooks.py)")
        return
    try:
        rc = git_hooks.install(target_root, quiet=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  secret-scan hook : SKIPPED ({exc.__class__.__name__}: {exc})")
        return
    if rc == 0:
        print(f"  secret-scan hook : {git_hooks.hook_path(target_root)}")
    else:
        print("  secret-scan hook : NOT installed — an existing pre-commit hook is in "
              "the way. Inspect it, then run:")
        print("                     python .claude/apiary/launch.py "
              "scripts/install_git_hooks.py --force")


def _ensure_gitignore_entry(target_root: Path) -> None:
    """Ensure ``<target>/.gitignore`` excludes the apiary-managed ``.claude/``.

    Idempotent, and never rewrites entries that are already there: a repo
    bootstrapped before #T-2026-258 keeps its blanket ``.claude/`` line rather
    than having its .gitignore edited underneath it. That form still excludes
    the right things — it just can't re-include repo-owned commands — so this
    prints how to widen it instead of doing it unasked.

    A .gitignore with no .claude entry at all (new or pre-existing) gets the
    stepwise block.
    """
    gi = target_root / ".gitignore"
    if gi.is_file():
        existing = gi.read_text(encoding="utf-8")
        lines = [line.strip() for line in existing.splitlines()]
        if any(line in _GITIGNORE_PRESENT for line in lines):
            if any(line in _GITIGNORE_BLANKET for line in lines):
                print(
                    "  .gitignore       : has a blanket `.claude/` entry; slash "
                    "commands this repo owns cannot be tracked under it.\n"
                    "                     To allow them, replace that line with:\n"
                    "                       .claude/*\n"
                    "                       !.claude/commands/\n"
                    "                       .claude/commands/*\n"
                    "                       !.claude/commands/<name>.md"
                )
            return
        new_text = existing
        if not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "\n" + _GITIGNORE_BLOCK
    else:
        new_text = _GITIGNORE_BLOCK
    gi.write_text(new_text, encoding="utf-8")


def _write_bootstrap_state(
    state_dir: Path,
    profile: str,
    apiary_version: str,
    settings_json_hash: str,
    commands_dir_hashes: dict[str, str],
    claude_md_zone_hash: str,
    is_first_install: bool,
    now_iso: str,
) -> None:
    """Write ``<state_dir>/bootstrap_state.json`` for drift detection."""
    p = state_dir / "bootstrap_state.json"
    existing = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    payload = {
        "schema_version": _BOOTSTRAP_STATE_SCHEMA,
        "profile": profile,
        "apiary_version": apiary_version,
        "settings_json_hash": settings_json_hash,
        "commands_dir_hashes": commands_dir_hashes,
        "claude_md_zone_hash": claude_md_zone_hash,
        "bootstrapped_at": existing.get("bootstrapped_at", now_iso) if not is_first_install else now_iso,
        "last_updated_at": now_iso,
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def _hash_file(p: Path) -> str:
    """Return sha256 hex of *p*'s contents."""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
