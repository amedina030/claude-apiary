"""Per-target state resolver and registry for centralized apiary state.

Replaces the previous "state lives at <target>/.apiary/" model with a
centralized layout under the apiary checkout::

    <apiary>/.repos/<name>-<id>/        # per-target state (scribe, runner, ...)
    <apiary>/.repos/registry.json       # id -> {name, real_path, timestamps, verified_ok}
    <apiary>/.repos/next_id             # monotonic counter, never reused

Each registered target gets a tiny breadcrumb back at
``<target>/.apiary/pointer`` so discovery is bidirectional.

The resolver is lazy: the first state-touching CLI call from a new path
auto-registers it. Apiary's own checkout self-registers as
``claude-apiary-<id>`` like any other target.

Spec: scribe note C-2026-46.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow `python core/utils/state.py` (e.g. via the launcher CLI mode) to
# resolve the `core.*` imports below — Python only puts the script's own
# directory on sys.path, not the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.utils.filelock import FileLock  # noqa: E402

REPOS_DIRNAME = ".repos"
REGISTRY_FILENAME = "registry.json"
NEXT_ID_FILENAME = "next_id"
POINTER_DIRNAME = ".apiary"
POINTER_FILENAME = "pointer"

# main-apiary's own version pin. Single-line semver. Read on every session
# open in a bootstrapped repo to compare against the repo's own version.json
# and trigger `apiary update` on mismatch (see MIGRATION-PLAN.md §7.5).
VERSION_FILE = "VERSION"
DEFAULT_APIARY_VERSION = "0.1.0"

# Per-repo pin-model files (introduced by per-repo migration; see MIGRATION-PLAN.md §5–§6).
# Each bootstrapped repo carries three small JSON files under <repo>/.claude/apiary/
# identifying main-apiary, recording its own current path, and pinning a version.
PIN_DIRNAME = ".claude/apiary"
SELF_POINTER_FILENAME = "self-pointer.json"
MAIN_APIARY_POINTER_FILENAME = "main-apiary-pointer.json"
VERSION_FILENAME = "version.json"
PIN_SCHEMA_VERSION = 1

TARGET_STATE_DIR_ENV = "APIARY_TARGET_STATE_DIR"

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_repo_root(start: Path | None = None) -> Path | None:
    """Return the git repo root containing *start* (or cwd), or None.

    Mirrors the helper that already exists in scribe/captures/researcher —
    centralized here so the resolver can stand alone.
    """
    cwd = str(start) if start is not None else str(Path.cwd())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


def _safe_name(raw: str) -> str:
    """Sanitize a basename for use in a folder name. Falls back to ``repo``
    when stripping leaves nothing usable so we always have a non-empty stem."""
    cleaned = _NAME_SAFE_RE.sub("-", raw).strip("-._")
    return cleaned or "repo"


def repos_dir(apiary_repo: Path) -> Path:
    return Path(apiary_repo) / REPOS_DIRNAME


def registry_path(apiary_repo: Path) -> Path:
    return repos_dir(apiary_repo) / REGISTRY_FILENAME


def next_id_path(apiary_repo: Path) -> Path:
    return repos_dir(apiary_repo) / NEXT_ID_FILENAME


def _load_registry(apiary_repo: Path) -> dict:
    """Read registry.json. Returns ``{}`` for missing/malformed files —
    callers must tolerate an empty registry on first use."""
    p = registry_path(apiary_repo)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_registry(apiary_repo: Path, data: dict) -> None:
    """Atomic write via .tmp + os.replace so concurrent readers never see
    a half-written file."""
    p = registry_path(apiary_repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def allocate_next_id(apiary_repo: Path) -> int:
    """Allocate a fresh monotonic UID by reading and bumping the counter.

    Public API. The single allocator for all UID-needing code paths:
    - ``resolve_target_state_dir`` (lazy auto-registration)
    - ``apiary install --target <repo>`` (per-repo bootstrap, post-migration)
    - drift handler's copy-detection branch (``register_copy`` mailbox flow)
    - mailbox processor (``register_copy`` message handling)

    Never call a parallel ID generator — the monotonic-only contract relies
    on a single source of allocations. New UIDs only ever increase, so they
    never collide with existing or historical entries.

    Caller MUST hold the registry FileLock — counter writes are not
    separately locked, and a concurrent allocator would otherwise issue
    duplicate UIDs.
    """
    p = next_id_path(apiary_repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = 0
    if p.is_file():
        try:
            current = int(p.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            current = 0
    new_id = current + 1
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(str(new_id) + "\n", encoding="utf-8")
    tmp.replace(p)
    return new_id


def _write_pointer(target_repo: Path, apiary_repo: Path, target_id: str) -> Path:
    """Write ``<target>/.apiary/pointer`` with the registry mapping. Atomic."""
    pointer_dir = Path(target_repo) / POINTER_DIRNAME
    pointer_dir.mkdir(parents=True, exist_ok=True)
    p = pointer_dir / POINTER_FILENAME
    payload = {
        "apiary_repo": str(Path(apiary_repo).resolve()),
        "target_id": target_id,
        "registered_at": _now_iso(),
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def _read_pointer(target_repo: Path) -> dict | None:
    """Return the pointer payload at ``<target>/.apiary/pointer``, or None.

    Tolerant of a missing or malformed file — both return None."""
    p = Path(target_repo) / POINTER_DIRNAME / POINTER_FILENAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _find_entry_by_path(registry: dict, real_path: Path) -> tuple[str, dict] | None:
    """Return (id_str, entry) for the first registry entry whose
    ``real_path`` matches *real_path*, or None."""
    target = str(Path(real_path).resolve())
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("real_path") == target:
            return id_str, entry
    return None


def resolve_apiary_repo(explicit: Path | None = None) -> Path:
    """Return the apiary checkout root.

    Resolution order (post-migration):

    1. ``explicit`` — caller supplied the path (CLI ``--apiary-repo`` flag).
    2. ``APIARY_MAIN_REPO`` env var — set by the per-repo launcher when
       it dispatches a script.
    3. ``__file__``-based: this module lives at ``<main-apiary>/core/utils/``,
       so its grandparent is main-apiary's root. This works whenever the
       resolver is called from inside main-apiary's own source tree.
    4. ``main-apiary-pointer.json`` at ``<cwd>/.claude/apiary/`` — set by
       ``apiary install`` for any bootstrapped repo, so callers running
       from inside a bootstrapped repo can locate main-apiary.

    Raises RuntimeError when none of the above resolve to a directory.
    """
    if explicit is not None:
        return Path(explicit).resolve()

    env_path = os.environ.get("APIARY_MAIN_REPO", "").strip()
    if env_path and Path(env_path).is_dir():
        return Path(env_path).resolve()

    self_repo = _REPO_ROOT
    if (self_repo / "core" / "install.py").is_file() and (self_repo / "VERSION").is_file():
        return self_repo

    pin = Path.cwd() / ".claude" / "apiary" / "main-apiary-pointer.json"
    if pin.is_file():
        try:
            data = json.loads(pin.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        candidate = Path(data.get("main_apiary_path", ""))
        if candidate.is_dir():
            return candidate.resolve()

    raise RuntimeError(
        "Cannot locate apiary repo. Pass --apiary-repo, set APIARY_MAIN_REPO, "
        "or run from inside main-apiary or a bootstrapped repo."
    )


def resolve_target_state_dir(
    cwd: Path | None = None,
    *,
    apiary_repo: Path | None = None,
    auto_register: bool = True,
) -> Path:
    """Return the per-target state directory under ``<apiary>/.repos/``.

    Resolves *cwd* (default: current working directory) to its git repo
    root, looks it up in the registry, and returns
    ``<apiary>/.repos/<name>-<id>/``. When the path is not yet registered
    and *auto_register* is True, allocates a new id, creates the directory,
    appends a registry entry, and writes a pointer file back into the
    target repo.

    Raises RuntimeError when *cwd* is not inside a git repo (no implicit
    fallback — callers that want one must check first), or when the apiary
    repo cannot be located.
    """
    apiary = resolve_apiary_repo(apiary_repo)
    start = Path(cwd) if cwd is not None else Path.cwd()
    target_root = _git_repo_root(start)
    if target_root is None:
        raise RuntimeError(
            f"Not inside a git repository: {start}. "
            f"Apiary state requires a git repo to identify the target."
        )
    target_root = target_root.resolve()

    repos_root = repos_dir(apiary)
    repos_root.mkdir(parents=True, exist_ok=True)

    with FileLock(registry_path(apiary)):
        registry = _load_registry(apiary)
        match = _find_entry_by_path(registry, target_root)
        if match is not None:
            id_str, entry = match
            entry["last_used"] = _now_iso()
            registry[id_str] = entry
            _save_registry(apiary, registry)
            folder_name = f"{entry['name']}-{id_str}"
            return repos_root / folder_name

        if not auto_register:
            raise RuntimeError(
                f"Target not registered: {target_root}. "
                f"Auto-registration disabled."
            )

        new_id = allocate_next_id(apiary)
        name = _safe_name(target_root.name)
        folder_name = f"{name}-{new_id}"
        state_dir = repos_root / folder_name
        state_dir.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        registry[str(new_id)] = {
            "name": name,
            "real_path": str(target_root),
            "uid": new_id,
            "version": read_apiary_version(apiary),
            "registered_at": now,
            "last_used": now,
            "verified_ok": True,
        }
        _save_registry(apiary, registry)
        try:
            _write_pointer(target_root, apiary, folder_name)
        except OSError:
            # Pointer write failures are non-fatal — the registry is the
            # source of truth and the breadcrumb is best-effort.
            pass
        return state_dir


def state_dir_from_env() -> Optional[Path]:
    """Return the state dir from ``APIARY_TARGET_STATE_DIR``, or None.

    Used by tools that want to honor the launcher's pre-resolved value
    without re-running the registry lookup."""
    value = os.environ.get(TARGET_STATE_DIR_ENV, "").strip()
    if not value:
        return None
    return Path(value)


def find_state_dir(target_repo: Path) -> Optional[Path]:
    """Return the per-target state directory for ``target_repo``, or None.

    Read-only resolver — does not allocate ids, write registry entries,
    or run git. Reads the repo's ``.claude/apiary/*-pointer.json`` pins
    (falling back to the legacy ``.apiary/pointer`` breadcrumb) and returns
    ``<apiary>/.repos/<name>-<uid>/`` if it exists on disk.

    Use this from passive consumers (GUI, dashboards, audits) that need
    to find an already-registered target's state without side effects.
    Targets that have never been registered, or whose pointer write
    failed, return None — callers may fall back to a legacy in-repo
    path if they want to support unmigrated targets.
    """
    repo = Path(target_repo)
    # Live pin model first: <repo>/.claude/apiary/{main-apiary-pointer,
    # self-pointer}.json -> <main-apiary>/.repos/<name>-<uid>/. Bootstrapped
    # repos carry no .apiary/pointer any more (that model was retired), so
    # without this branch launcher-less callers could never find their
    # state dir and fell through to whatever fallback they had.
    main_ptr = read_main_apiary_pointer(repo)
    self_ptr = read_self_pointer(repo)
    if main_ptr and self_ptr:
        main_path = main_ptr.get("main_apiary_path", "")
        name, uid = self_ptr.get("name", ""), self_ptr.get("uid", "")
        if main_path and name and uid != "":
            state_dir = Path(main_path) / REPOS_DIRNAME / f"{name}-{uid}"
            if state_dir.is_dir():
                return state_dir
    # Legacy breadcrumb model (<repo>/.apiary/pointer).
    pointer = _read_pointer(repo)
    if pointer is None:
        return None
    apiary_str = pointer.get("apiary_repo", "")
    target_id = pointer.get("target_id", "")
    if not apiary_str or not target_id:
        return None
    state_dir = Path(apiary_str) / REPOS_DIRNAME / target_id
    return state_dir if state_dir.is_dir() else None


def read_apiary_version(apiary_repo: Path) -> str:
    """Return main-apiary's pinned version (the contents of ``<apiary>/VERSION``).

    Falls back to ``DEFAULT_APIARY_VERSION`` when the file is missing or
    empty — this matches phase 0's "all repos start at 0.1.0" baseline and
    avoids forcing every caller to handle a missing-VERSION case during
    the migration window.
    """
    p = Path(apiary_repo) / VERSION_FILE
    if not p.is_file():
        return DEFAULT_APIARY_VERSION
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_APIARY_VERSION
    return text or DEFAULT_APIARY_VERSION


# --- per-repo pin-model helpers -------------------------------------------------
# These read/write the three small JSON files that each bootstrapped repo
# carries under <repo>/.claude/apiary/. See MIGRATION-PLAN.md §5–§6 for
# schemas. Read helpers tolerate missing/malformed files (return None) so
# callers can branch on "not yet bootstrapped" without try/except. Write
# helpers are atomic (.tmp + os.replace) and create parent dirs.


def pin_dir(repo: Path) -> Path:
    """Return ``<repo>/.claude/apiary/`` — the per-repo pin-model directory."""
    return Path(repo) / PIN_DIRNAME


def self_pointer_path(repo: Path) -> Path:
    return pin_dir(repo) / SELF_POINTER_FILENAME


def main_apiary_pointer_path(repo: Path) -> Path:
    return pin_dir(repo) / MAIN_APIARY_POINTER_FILENAME


def version_path(repo: Path) -> Path:
    return pin_dir(repo) / VERSION_FILENAME


def _read_json_file(p: Path) -> dict | None:
    """Load a JSON object from *p*. Return None for missing/malformed files."""
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_file(p: Path, payload: dict) -> Path:
    """Atomic write of *payload* as JSON. Creates parent dirs."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def read_self_pointer(repo: Path) -> dict | None:
    """Return the parsed self-pointer for *repo*, or None.

    Schema (§6.3): ``{schema_version, uid, name, real_path, registered_at, last_drift_check}``.
    The ``real_path`` field is the source of truth for "where this repo
    thinks it lives." A mismatch with the repo's actual current path means
    drift — see §7.2 for the handler.
    """
    return _read_json_file(self_pointer_path(repo))


def write_self_pointer(repo: Path, payload: dict) -> Path:
    """Atomically write the self-pointer for *repo*.

    Caller supplies the dict; this helper does not validate field shape
    beyond injecting ``schema_version`` if absent. Bootstrap and the drift
    handler are the two callers expected post-migration.

    **Critical:** the file MUST NOT be committed to git — every clone would
    inherit the original's path and the drift handler would treat the clone
    as the original-that-moved. ``apiary install`` writes ``.claude/`` into
    the repo's ``.gitignore``; do not relax that.
    """
    payload = {"schema_version": PIN_SCHEMA_VERSION, **payload}
    return _write_json_file(self_pointer_path(repo), payload)


def read_main_apiary_pointer(repo: Path) -> dict | None:
    """Return the parsed main-apiary-pointer for *repo*, or None.

    Schema (§6.2): ``{schema_version, main_apiary_path, main_apiary_uid, registered_at}``.
    The ``main_apiary_path`` is absolute and machine-specific; updated by
    main-apiary's cascade-fix when main-apiary itself moves (§7.3).
    """
    return _read_json_file(main_apiary_pointer_path(repo))


def write_main_apiary_pointer(repo: Path, payload: dict) -> Path:
    """Atomically write the main-apiary-pointer for *repo*."""
    payload = {"schema_version": PIN_SCHEMA_VERSION, **payload}
    return _write_json_file(main_apiary_pointer_path(repo), payload)


def read_version(repo: Path) -> dict | None:
    """Return the parsed version pin for *repo*, or None.

    Schema (§6.4): ``{schema_version, apiary_version, pinned_at}``. The
    ``apiary_version`` is semver; compared against ``<main-apiary>/VERSION``
    on every session open to detect drift.
    """
    return _read_json_file(version_path(repo))


def write_version(repo: Path, payload: dict) -> Path:
    """Atomically write the version pin for *repo*."""
    payload = {"schema_version": PIN_SCHEMA_VERSION, **payload}
    return _write_json_file(version_path(repo), payload)


if __name__ == "__main__":
    # Print the per-target state dir for the cwd. Used by skill templates
    # and shell snippets that need the path without re-implementing the
    # registry lookup. Invoked via:
    #   python ~/.claude/apiary_launch.py core/utils/state.py
    try:
        print(resolve_target_state_dir())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
