"""``apiary install --target <repo>`` — per-repo bootstrap.

Generates the per-repo files under ``<repo>/.claude/apiary/`` (launcher,
pointers, version, flags/, session-tmp/), writes a ``settings.json`` whose
hooks dispatch through the per-repo launcher, copies slash commands, and
updates the registry. Idempotent — re-running refreshes generated files
without disturbing user-owned content.

See ``docs/architecture/per-repo-install.md`` for the full step list.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import context_rules as cr
from core import launcher_template
from core.hooks_lib import load_settings, register_hooks, save_settings
from core.utils import state
from core.utils.atomic import write_json_atomic, write_text_atomic
from core.utils.filelock import FileLock

# Top-level keys in <repo>/.claude/settings.json that apiary owns outright:
# they are regenerated from scratch on every install and a hand edit inside
# them is replaced. `hooks` is the whole set — `register_hooks` rewrites the
# apiary-marked entries there and leaves the user's alone.
#
# Every OTHER key the profile carries belongs to the user's file. The profile's
# values are merged into it (see _merge_profile_value): user entries survive,
# apiary's are added, and an entry apiary contributed last time but the profile
# no longer ships is pruned (see _prune_stale_profile_values). Anything the
# profile never mentions is left exactly as the user wrote it.
_APIARY_OWNED_KEYS = ("hooks",)

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
    2. Under registry FileLock: find, re-adopt or allocate a uid, update the
       registry entry, ensure the per-target state dir exists.
    3. Generate per-repo files under ``<repo>/.claude/apiary/``. The
       self-pointer is rewritten from the registry every time, so a pin left
       disagreeing with it cannot survive an install.
    4. Write ``<repo>/.claude/settings.json``: apiary's hook entries (marked,
       so the user's own hooks are untouched) pointing at the per-repo
       launcher, plus the profile merged into the user's other keys.
    5. Copy slash commands into ``<repo>/.claude/commands/``.
    6. Write the apiary-managed zone into ``<repo>/CLAUDE.md``.
    7. Add ``.claude/`` to ``<repo>/.gitignore`` if not already present.
    8. Write ``bootstrap_state.json`` with hashes of generated files.

    Returns :class:`InstallResult` describing what was done.
    """
    target_root = _resolve_target(target_repo)
    apiary = state.resolve_apiary_repo(apiary_repo)
    apiary = apiary.resolve()

    pinned = state.read_self_pointer(target_root)
    uid, name, is_first, registered_at = _register_or_update(target_root, apiary, pinned)
    slug = f"{name}-{uid}"
    state_dir = state.repos_dir(apiary) / slug
    state_dir.mkdir(parents=True, exist_ok=True)
    _scaffold_scribe_templates(state_dir)
    previous = _read_bootstrap_state(state_dir)

    apiary_version = state.read_apiary_version(apiary)
    now = state.now_iso()

    # Per-repo .claude/apiary/ files
    pin = state.pin_dir(target_root)
    pin.mkdir(parents=True, exist_ok=True)
    (pin / "flags").mkdir(exist_ok=True)
    (pin / "session-tmp").mkdir(exist_ok=True)
    _write_launcher(pin / "launch.py")
    state.write_main_apiary_pointer(
        target_root,
        {
            "main_apiary_path": str(apiary),
            "main_apiary_uid": state.MAIN_APIARY_UID,
            "registered_at": registered_at,
        },
    )
    # Always rewritten, never merely created: an existing pin that disagrees
    # with the registry is the Bug 4 state — the launcher builds a state-dir
    # name from the pinned uid, so a stale one silently reroutes every tool's
    # state to a directory nothing else knows about. The registry is the
    # source of truth; only the drift timestamp is carried over.
    state.write_self_pointer(
        target_root,
        {
            "uid": uid,
            "name": name,
            "real_path": str(target_root),
            "registered_at": registered_at,
            "last_drift_check": (pinned or {}).get("last_drift_check", now),
        },
    )
    state.write_version(
        target_root,
        {
            "apiary_version": apiary_version,
            "pinned_at": now,
        },
    )

    # Per-repo settings.json with hooks pointing at per-repo launcher
    settings_hash, profile_settings = _write_per_repo_settings(
        target_root,
        apiary,
        profile,
        _dict_field(previous, "profile_settings"),
    )

    # Slash commands
    commands_hashes = _copy_slash_commands(
        target_root, apiary, _dict_field(previous, "commands_dir_hashes")
    )

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
    _write_bootstrap_state(
        state_dir,
        profile,
        apiary_version,
        settings_hash,
        commands_hashes,
        claude_md_hash,
        profile_settings,
        previous,
        is_first,
        now,
    )

    return InstallResult(
        uid=uid,
        name=name,
        slug=slug,
        target_repo=target_root,
        apiary_repo=apiary,
        state_dir=state_dir,
        apiary_version=apiary_version,
        is_first_install=is_first,
    )


# --- Registry interaction -------------------------------------------------


def _resolve_target(target_repo: Path) -> Path:
    target = Path(target_repo).resolve()
    if not target.is_dir():
        raise InstallError(f"target {target} is not a directory")
    root = state.git_root(target)
    if root is None:
        raise InstallError(
            f"target {target} is not inside a git repository — "
            "apiary requires a git repo to identify the target"
        )
    return root.resolve()


def _readoptable_uid(pinned: dict | None, registry: dict) -> int | None:
    """The uid in *pinned* if the registry can still hand it back, else None.

    ``.repos/`` is gitignored, so a fresh clone of main-apiary starts with an
    empty registry while every bootstrapped repo still carries its self-pointer.
    Allocating a new uid there would strand ``.repos/<name>-<old-uid>/`` — the
    repo's entire scribe/compass history — as an orphan, so the pinned uid is
    re-adopted whenever it is free. A uid another repo already holds is not.
    """
    if not isinstance(pinned, dict):
        return None
    try:
        uid = int(pinned.get("uid", -1))
    except (TypeError, ValueError):
        return None
    if uid < 1 or str(uid) in registry:
        return None
    return uid


def _register_or_update(
    target_root: Path,
    apiary: Path,
    pinned: dict | None = None,
) -> tuple[int, str, bool, str]:
    """Return (uid, name, is_first_install, registered_at_iso). Holds the
    registry FileLock for the read-update-write.

    *pinned* is the target's existing self-pointer, if any — consulted only
    when the registry has no entry for this path (see :func:`_readoptable_uid`).
    """
    repos_root = state.repos_dir(apiary)
    repos_root.mkdir(parents=True, exist_ok=True)
    now = state.now_iso()
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
            readopted = _readoptable_uid(pinned, registry)
            if readopted is not None:
                uid = readopted
                # The pinned name comes back with the uid: the state dir is
                # <name>-<uid>, so a rename since the original registration
                # would otherwise orphan it. This is also what the lost entry
                # said — the by-path branch above never renames either.
                name = state._safe_name(str((pinned or {}).get("name") or target_root.name))
                # The counter is usually lost with the registry, so raise it
                # past the re-adopted uid or a later install gets the same one.
                state.reserve_uid(apiary, uid)
            else:
                uid = state.allocate_next_id(apiary)
                name = state._safe_name(target_root.name)
            uid_str = str(uid)
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
    write_text_atomic(launcher_path, launcher_template.LAUNCHER_PY)
    try:
        launcher_path.chmod(launcher_path.stat().st_mode | 0o755)
    except OSError:
        pass  # best-effort — not all filesystems support chmod


def _write_per_repo_settings(
    target_root: Path,
    apiary: Path,
    profile: str,
    previous_profile: dict | None = None,
) -> tuple[str, dict]:
    """Generate ``<target>/.claude/settings.json`` with hooks dispatched
    through the per-repo launcher.

    Returns ``(sha256 of the written file, the profile values applied)``. The
    second element is recorded in ``bootstrap_state.json`` and handed back as
    *previous_profile* on the next install so entries the profile has since
    dropped can be pruned.

    One entry per event, each pointing at ``core/hooks/dispatch.py <verb>``
    via ``$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py``. The individual hooks
    and their matchers live in the dispatcher's registry, not here (review
    X-1); ``core/hooks_factory`` builds the entries.
    """
    from core import hooks_factory

    hooks = hooks_factory.build_dispatch_hooks()

    settings_path = target_root / ".claude" / "settings.json"
    # `hooks` is the one apiary-owned key: register_hooks replaces every
    # marked entry and leaves the user's own hooks in place.
    register_hooks(settings_path, hooks)

    # Everything else the profile carries is merged into the user's file.
    applied = _apply_profile_settings(settings_path, apiary, profile, previous_profile)

    return _hash_file(settings_path), applied


def _apply_profile_settings(
    settings_path: Path,
    apiary: Path,
    profile: str,
    previous_profile: dict | None = None,
) -> dict:
    """Merge the resolved profile into *settings_path*; return what it applied.

    The profile is the source of truth for what *apiary* contributes, and the
    file is the source of truth for everything else. So, per non-owned key:

    - values the user put there are never overwritten (Bug 7: a re-install used
      to replace ``permissions`` wholesale, discarding the user's entries),
    - the profile's values are added,
    - values the previous install contributed and this profile no longer ships
      are pruned, so a permission can be withdrawn without editing every repo.

    No interactive drift prompt — the install contract is trusted (idempotent).
    """
    from core.apiary_profiles import resolve

    resolved = resolve(profile, apiary / "profiles")
    applied = {k: v for k, v in resolved.merged.items() if k not in _APIARY_OWNED_KEYS}
    settings = load_settings(settings_path)
    _prune_stale_profile_values(settings, previous_profile or {}, applied)
    for key, value in applied.items():
        settings[key] = _merge_profile_value(settings.get(key, _MISSING), value)
    save_settings(settings_path, settings)
    return applied


_MISSING = object()


def _merge_profile_value(user: Any, profile_value: Any) -> Any:
    """Return *user*'s value with *profile_value* merged in, user winning.

    Dicts merge key by key, lists concatenate (user's order first, profile
    entries the user does not already have appended), and for anything else
    the user's value stands — apiary never silently rewrites a scalar somebody
    chose. A key the user does not have yet is taken from the profile.
    """
    if user is _MISSING:
        return copy.deepcopy(profile_value)
    if isinstance(user, dict) and isinstance(profile_value, dict):
        merged = dict(user)
        for key, value in profile_value.items():
            merged[key] = _merge_profile_value(merged.get(key, _MISSING), value)
        return merged
    if isinstance(user, list) and isinstance(profile_value, list):
        return user + [v for v in profile_value if v not in user]
    return user


def _prune_stale_profile_values(user: Any, previous: Any, current: Any) -> None:
    """Drop values a previous install contributed that *current* no longer has.

    Mutates *user* in place. Only ever removes something byte-identical to what
    the last install wrote (``previous``), so a value the user has since edited
    is left alone — the same rule ``_copy_slash_commands`` uses for commands.
    """
    if isinstance(user, dict) and isinstance(previous, dict):
        cur = current if isinstance(current, dict) else {}
        for key, prev_value in previous.items():
            if key not in user:
                continue
            if key in cur:
                _prune_stale_profile_values(user[key], prev_value, cur[key])
            elif user[key] == prev_value:
                del user[key]
        return
    if isinstance(user, list) and isinstance(previous, list):
        cur = current if isinstance(current, list) else []
        stale = [v for v in previous if v not in cur]
        if stale:
            user[:] = [v for v in user if v not in stale]


def _copy_slash_commands(
    target_root: Path, apiary: Path, previous_hashes: dict[str, str] | None = None
) -> dict[str, str]:
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


def _bootstrap_state_path(state_dir: Path) -> Path:
    return Path(state_dir) / "bootstrap_state.json"


def _read_bootstrap_state(state_dir: Path) -> dict:
    """The record the previous install left, or ``{}`` when there is none.

    A file that exists but cannot be read is an :class:`InstallError`, not an
    empty dict: it is what tells this install which commands and profile values
    apiary contributed last time, so silently treating it as absent would strand
    every one of them in the target repo (Bug 10).
    """
    p = _bootstrap_state_path(state_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(
            f"{p} is unreadable ({exc.__class__.__name__}: {exc}). Delete it and "
            "re-run `apiary install` to rebuild it from the current install."
        ) from exc
    if not isinstance(data, dict):
        raise InstallError(f"{p} is not a JSON object. Delete it and re-run `apiary install`.")
    return data


def _dict_field(payload: dict, key: str) -> dict:
    """*payload*'s ``key`` when it is a dict, else ``{}``."""
    value = payload.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _slash_command_sources(apiary: Path) -> Iterable[Path]:
    """Yield every ``<tool>/commands/*.md`` path under main-apiary."""
    for tool in (
        "budgeter",
        "scribe",
        "core",
        "docs",
        "refiner",
        "harden",
        "compass",
        "researcher",
        "runner",
        "incubator",
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

    A CLAUDE.md whose sentinels have been tampered with, or that cannot be
    read/written, raises :class:`InstallError` rather than the underlying
    exception: this runs after the registry entry, pin files and settings.json
    are already written, so the operator needs the one line that says what to
    fix, not a traceback out of the middle of an install (Bug 10).
    """
    rules_dir = apiary / "context-rules"
    if not rules_dir.is_dir():
        return ""  # nothing to install — skip cleanly

    rules = cr.load_all_rules(rules_dir)
    rendered_zone = cr.render_managed_zone(rules)

    target = target_root / "CLAUDE.md"
    try:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    except OSError as exc:
        raise InstallError(f"could not read {target} ({exc.__class__.__name__}: {exc})") from exc
    try:
        existing_zone = cr.find_managed_zone(existing) if existing else None
    except cr.ZoneTamperError as exc:
        raise InstallError(
            f"{target}: the apiary-managed zone is malformed ({exc}). Repair the "
            f"`{cr.OUTER_START}` / `{cr.OUTER_END}` sentinels — or delete the zone "
            "entirely and let this install rewrite it — then re-run `apiary install`."
        ) from exc

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

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged, encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"could not write {target} ({exc.__class__.__name__}: {exc})") from exc
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
_GITIGNORE_PRESENT = (".claude/", "/.claude/", ".claude", "/.claude", ".claude/*", "/.claude/*")

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
        from scribe.paths import SCRIBE_SUBDIR
        from scribe.templates import scaffold_defaults
    except Exception as exc:  # noqa: BLE001 - never fail an install over this
        print(f"  scribe templates : SKIPPED (import failed: {exc})")
        return
    try:
        written = scaffold_defaults(state_dir / SCRIBE_SUBDIR)
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
        print(
            "  secret-scan hook : NOT installed — an existing pre-commit hook is in "
            "the way. Inspect it, then run:"
        )
        print(
            "                     python .claude/apiary/launch.py "
            "scripts/install_git_hooks.py --force"
        )


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
    profile_settings: dict,
    existing: dict,
    is_first_install: bool,
    now_iso: str,
) -> None:
    """Write ``<state_dir>/bootstrap_state.json`` for drift detection.

    *existing* is the record read at the start of this install (already
    validated by :func:`_read_bootstrap_state`), so nothing re-parses the file
    here and a mid-install rewrite cannot smuggle in a bad ``bootstrapped_at``.
    """
    p = _bootstrap_state_path(state_dir)
    payload = {
        "schema_version": _BOOTSTRAP_STATE_SCHEMA,
        "profile": profile,
        "apiary_version": apiary_version,
        "settings_json_hash": settings_json_hash,
        "commands_dir_hashes": commands_dir_hashes,
        "claude_md_zone_hash": claude_md_zone_hash,
        # What the profile contributed to settings.json, so the next install
        # can withdraw an entry the profile has stopped shipping.
        "profile_settings": profile_settings,
        "bootstrapped_at": existing.get("bootstrapped_at", now_iso)
        if not is_first_install
        else now_iso,
        "last_updated_at": now_iso,
    }
    try:
        write_json_atomic(p, payload, indent=2, sort_keys=True, trailing_newline=True)
    except OSError as exc:
        raise InstallError(
            f"could not write {p} ({exc.__class__.__name__}: {exc}). The repo is "
            "installed; re-run `apiary install` once the path is writable so "
            "`apiary doctor stale` can detect drift."
        ) from exc


def _hash_file(p: Path) -> str:
    """Return sha256 hex of *p*'s contents."""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
