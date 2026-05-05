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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.utils.apiary_pointer import get_repo_path
from core.utils.filelock import FileLock

REPOS_DIRNAME = ".repos"
REGISTRY_FILENAME = "registry.json"
NEXT_ID_FILENAME = "next_id"
POINTER_DIRNAME = ".apiary"
POINTER_FILENAME = "pointer"

TARGET_STATE_DIR_ENV = "APIARY_TARGET_STATE_DIR"
LEGACY_LAYOUT_ENV = "APIARY_STATE_LAYOUT"

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


def _allocate_next_id(apiary_repo: Path) -> int:
    """Read-and-bump the monotonic id counter. Caller MUST hold the
    registry FileLock — counter writes are not separately locked."""
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
    """Return the apiary checkout root. Prefers *explicit*, falls back to
    the global ``~/.claude/apiary.json`` pointer. Raises RuntimeError when
    neither is available — the resolver cannot operate without it."""
    if explicit is not None:
        return Path(explicit).resolve()
    repo = get_repo_path()
    if repo is None:
        raise RuntimeError(
            "Cannot locate apiary repo: no ~/.claude/apiary.json pointer. "
            "Run setup.py --global to install the pointer, or pass an explicit path."
        )
    return repo


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

        new_id = _allocate_next_id(apiary)
        name = _safe_name(target_root.name)
        folder_name = f"{name}-{new_id}"
        state_dir = repos_root / folder_name
        state_dir.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        registry[str(new_id)] = {
            "name": name,
            "real_path": str(target_root),
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


def is_legacy_layout() -> bool:
    """Return True when the legacy layout escape hatch is set."""
    return os.environ.get(LEGACY_LAYOUT_ENV, "").strip().lower() == "legacy"
