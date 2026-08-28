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

import os
import re
import sys
from pathlib import Path
from typing import Optional

# Allow `python core/utils/state.py` (e.g. via the launcher CLI mode) to
# resolve the `core.*` imports below — Python only puts the script's own
# directory on sys.path, not the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.utils.atomic import write_json_atomic, write_text_atomic  # noqa: E402
from core.utils.filelock import FileLock  # noqa: E402
from core.utils.gitutil import git_root, main_worktree_root  # noqa: E402
from core.utils.jsonio import read_json_object  # noqa: E402
from core.utils.timeutil import now_iso  # noqa: E402

REPOS_DIRNAME = ".repos"
REGISTRY_FILENAME = "registry.json"
NEXT_ID_FILENAME = "next_id"
POINTER_DIRNAME = ".apiary"
POINTER_FILENAME = "pointer"

# main-apiary's own version pin. Single-line semver. Read on every session
# open in a bootstrapped repo to compare against the repo's own version.json.
VERSION_FILE = "VERSION"
DEFAULT_APIARY_VERSION = "0.1.0"

# Per-repo pin-model files (see docs/architecture/per-repo-install.md).
# Each bootstrapped repo carries three small JSON files under <repo>/.claude/apiary/
# identifying main-apiary, recording its own current path, and pinning a version.
PIN_DIRNAME = ".claude/apiary"
SELF_POINTER_FILENAME = "self-pointer.json"
MAIN_APIARY_POINTER_FILENAME = "main-apiary-pointer.json"
VERSION_FILENAME = "version.json"
PIN_SCHEMA_VERSION = 1

TARGET_STATE_DIR_ENV = "APIARY_TARGET_STATE_DIR"

# Main-apiary's UID is reserved at slot 1 by convention: it self-registers
# first, and `allocate_next_id` is monotonic, so nothing else can take it.
# Was a literal `1` in core/drift.py, core/cascade.py and core/install.py
# (review finding X-3).
MAIN_APIARY_UID = 1

# Legacy in-repo state root, `<repo>/.apiary/`. Pre-migration targets kept
# scribe/compass/researcher/captures state here; `resolve_state_dir` still
# falls back to it for anything never re-bootstrapped.
LEGACY_STATE_DIRNAME = POINTER_DIRNAME

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


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
    return read_json_object(registry_path(apiary_repo)) or {}


def _save_registry(apiary_repo: Path, data: dict) -> None:
    """Atomic write so concurrent readers never see a half-written file."""
    write_json_atomic(
        registry_path(apiary_repo),
        data,
        indent=2,
        sort_keys=True,
        trailing_newline=True,
    )


def allocate_next_id(apiary_repo: Path) -> int:
    """Allocate a fresh monotonic UID by reading and bumping the counter.

    Public API. The single allocator for all UID-needing code paths:
    - ``resolve_target_state_dir`` (lazy auto-registration)
    - ``apiary install --target <repo>`` (per-repo bootstrap, post-migration)
    - drift handler's copy-detection branch (``core/drift.py``)

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
    write_text_atomic(p, str(new_id) + "\n")
    return new_id


def reserve_uid(apiary_repo: Path, uid: int) -> None:
    """Raise the monotonic counter so *uid* can never be allocated again.

    ``apiary install`` re-adopts the uid recorded in a repo's self-pointer when
    the registry has lost that repo's entry (see ``install._readoptable_uid``)
    — usually because ``.repos/`` is gitignored and this is a fresh clone of
    main-apiary. The ``next_id`` counter is lost in the same breath, so without
    this the next allocation would hand the re-adopted uid to a different repo
    and both would share one state directory.

    No-op when the counter is already above *uid*. Caller MUST hold the
    registry FileLock, like :func:`allocate_next_id`.
    """
    p = next_id_path(apiary_repo)
    current = 0
    if p.is_file():
        try:
            current = int(p.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            current = 0
    if current >= uid:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(str(uid) + "\n", encoding="utf-8")
    tmp.replace(p)


def _write_pointer(target_repo: Path, apiary_repo: Path, target_id: str) -> Path:
    """Write ``<target>/.apiary/pointer`` with the registry mapping. Atomic."""
    p = Path(target_repo) / POINTER_DIRNAME / POINTER_FILENAME
    payload = {
        "apiary_repo": str(Path(apiary_repo).resolve()),
        "target_id": target_id,
        "registered_at": now_iso(),
    }
    write_json_atomic(p, payload, indent=2, trailing_newline=True)
    return p


def _read_pointer(target_repo: Path) -> dict | None:
    """Return the pointer payload at ``<target>/.apiary/pointer``, or None.

    Tolerant of a missing or malformed file — both return None."""
    return read_json_object(Path(target_repo) / POINTER_DIRNAME / POINTER_FILENAME)


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


def _pinned_main_apiary(repo: Path) -> Path | None:
    """Return the main-apiary *repo* is pinned to, or None.

    Reads ``<repo>/.claude/apiary/main-apiary-pointer.json`` — the pin
    ``apiary install`` writes into every bootstrapped repo, including
    main-apiary itself (which points at itself).
    """
    data = read_json_object(main_apiary_pointer_path(repo))
    if not data:
        return None
    candidate = Path(str(data.get("main_apiary_path", "")))
    if str(candidate) and candidate.is_dir():
        return candidate.resolve()
    return None


def resolve_apiary_repo(explicit: Path | None = None) -> Path:
    """Return the apiary checkout root — the one that owns ``.repos/``.

    Resolution order:

    1. ``explicit`` — caller supplied the path (CLI ``--apiary-repo`` flag).
    2. ``APIARY_MAIN_REPO`` env var — set by the per-repo launcher when
       it dispatches a script.
    3. ``main-apiary-pointer.json`` — the registry-anchored answer. Checked
       at ``<cwd>/.claude/apiary/`` first (the repo the caller is working
       in), then at the source tree this module was loaded from.
    4. The source tree itself, ``<main-apiary>/core/utils/`` → its
       grandparent, when it looks like a main-apiary checkout. A **linked
       git worktree resolves to its main checkout**, not to itself.

    Why 3 outranks 4 (review Phase 3.2): preferring the source tree meant
    any throwaway worktree of main-apiary — every agent worktree under
    ``.claude/worktrees/`` — became "main-apiary" for the duration, grew
    its own ``.repos/registry.json``, registered targets into it, and took
    that state to the grave when the worktree was pruned. The pin, and the
    de-worktreeing in step 4, both point at the registered main repo.

    Raises RuntimeError when none of the above resolve to a directory.
    """
    if explicit is not None:
        return Path(explicit).resolve()

    env_path = os.environ.get("APIARY_MAIN_REPO", "").strip()
    if env_path and Path(env_path).is_dir():
        return Path(env_path).resolve()

    for pinned_at in (Path.cwd(), _REPO_ROOT):
        pinned = _pinned_main_apiary(pinned_at)
        if pinned is not None:
            return pinned

    # No pin anywhere (a fresh clone that has never been self-bootstrapped).
    # Fall back to the source tree, collapsed onto its main checkout so a
    # worktree never stands in for the repo it was cut from.
    self_repo = main_worktree_root(_REPO_ROOT) or _REPO_ROOT
    if not _looks_like_apiary(self_repo):
        self_repo = _REPO_ROOT
    if _looks_like_apiary(self_repo):
        return self_repo.resolve()

    raise RuntimeError(
        "Cannot locate apiary repo. Pass --apiary-repo, set APIARY_MAIN_REPO, "
        "or run from inside main-apiary or a bootstrapped repo."
    )


def _looks_like_apiary(repo: Path) -> bool:
    """True when *repo* has the sentinel files of a main-apiary checkout."""
    return (repo / "core" / "install.py").is_file() and (repo / VERSION_FILE).is_file()


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
    target_root = git_root(start)
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
            entry["last_used"] = now_iso()
            registry[id_str] = entry
            _save_registry(apiary, registry)
            folder_name = f"{entry['name']}-{id_str}"
            return repos_root / folder_name

        if not auto_register:
            raise RuntimeError(f"Target not registered: {target_root}. Auto-registration disabled.")

        new_id = allocate_next_id(apiary)
        name = _safe_name(target_root.name)
        folder_name = f"{name}-{new_id}"
        state_dir = repos_root / folder_name
        state_dir.mkdir(parents=True, exist_ok=True)
        now = now_iso()
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


def _state_dir_from_pins(repo: Path) -> Optional[Path]:
    """``<main-apiary>/.repos/<name>-<uid>/`` from *repo*'s pin files.

    The live model: ``apiary install`` writes ``main-apiary-pointer.json``
    (where main-apiary is) and ``self-pointer.json`` (this repo's name and
    uid) under ``<repo>/.claude/apiary/``. Returns None unless both parse
    and the directory they name exists.
    """
    main_ptr = read_main_apiary_pointer(repo)
    self_ptr = read_self_pointer(repo)
    if not main_ptr or not self_ptr:
        return None
    main_path = main_ptr.get("main_apiary_path", "")
    name, uid = self_ptr.get("name", ""), self_ptr.get("uid", "")
    if not main_path or not name or uid == "" or uid is None:
        return None
    state_dir = Path(main_path) / REPOS_DIRNAME / f"{name}-{uid}"
    return state_dir if state_dir.is_dir() else None


def _state_dir_from_pointer(repo: Path) -> Optional[Path]:
    """``<apiary>/.repos/<target_id>/`` from the legacy ``.apiary/pointer``
    breadcrumb. Retired model, kept for targets never re-bootstrapped."""
    pointer = _read_pointer(repo)
    if pointer is None:
        return None
    apiary_str = pointer.get("apiary_repo", "")
    target_id = pointer.get("target_id", "")
    if not apiary_str or not target_id:
        return None
    state_dir = Path(apiary_str) / REPOS_DIRNAME / target_id
    return state_dir if state_dir.is_dir() else None


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

    :func:`resolve_state_dir` is the fuller resolver most callers want: it
    consults the launcher's env var first and can fall back to the legacy
    in-repo path.
    """
    repo = Path(target_repo)
    return _state_dir_from_pins(repo) or _state_dir_from_pointer(repo)


def resolve_state_dir(
    start: Path | None = None,
    *,
    subdir: str | None = None,
    repo: Path | None = None,
    use_env: bool = True,
    legacy_in_repo: bool = True,
    cwd_fallback: bool = False,
    require_exists: bool = False,
) -> Optional[Path]:
    """Return the apiary state directory for a target, or None.

    **The** state resolver. scribe, compass, researcher, captures, runner,
    the GUI aggregator and ``core.session`` each re-implemented some prefix
    of this precedence, and they disagreed about the tail (review X-3);
    they all call this now.

    Precedence, highest first:

    1. ``$APIARY_TARGET_STATE_DIR`` — exported by the per-repo launcher
       *after* it has done the registry lookup, so it is the pre-resolved
       answer and is never second-guessed. Skip with ``use_env=False``
       (the GUI aggregates several repos in one process and must resolve
       each by path, not by whichever repo launched it).
    2. The repo's pin files → ``<main-apiary>/.repos/<name>-<uid>/``.
    3. The legacy ``<repo>/.apiary/pointer`` breadcrumb → the same shape.
    4. ``<repo>/.apiary/`` itself — the pre-migration in-repo layout, for
       targets never re-bootstrapped. Skip with ``legacy_in_repo=False``
       (``core.session`` prefers an OS-temp fallback over growing an
       un-ignored ``.apiary/`` in a repo apiary does not manage).
    5. ``<start-or-cwd>/.apiary/`` when *start* is not inside a git repo
       at all and ``cwd_fallback=True`` — what the knowledge-store CLIs do
       so they still work in a plain directory.

    *repo* names the target directly and skips the ``git rev-parse`` that
    would otherwise resolve *start*; steps 2-5 use it as-is. *subdir* is
    appended to whatever is returned (``"scribe"``, ``"compass"``, …).
    ``require_exists=True`` rejects any candidate whose final path is not
    a directory and moves on to the next step, so a caller that only wants
    state that is really there does not have to re-check.

    With the defaults, the only ``None`` is "not inside a git repo"; the
    knowledge stores pass ``cwd_fallback=True`` and so never see one.
    """

    def _accept(base: Path | None) -> Optional[Path]:
        if base is None:
            return None
        candidate = base / subdir if subdir else base
        if require_exists and not candidate.is_dir():
            return None
        return candidate

    if use_env:
        found = _accept(state_dir_from_env())
        if found is not None:
            return found

    target = Path(repo) if repo is not None else git_root(start)
    if target is None:
        if cwd_fallback:
            base = Path(start) if start is not None else Path.cwd()
            return _accept(base / LEGACY_STATE_DIRNAME)
        return None
    target = Path(target)

    # Lazily: a repo on the live pin model must not pay for a breadcrumb read.
    for resolver in (_state_dir_from_pins, _state_dir_from_pointer):
        found = _accept(resolver(target))
        if found is not None:
            return found

    if legacy_in_repo:
        return _accept(target / LEGACY_STATE_DIRNAME)
    return None


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
# carries under <repo>/.claude/apiary/. See docs/architecture/per-repo-install.md
# for schemas. Read helpers tolerate missing/malformed files (return None) so
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


def _write_json_file(p: Path, payload: dict) -> Path:
    """Atomic write of *payload* as JSON. Creates parent dirs."""
    write_json_atomic(p, payload, indent=2, sort_keys=True, trailing_newline=True)
    return p


def read_self_pointer(repo: Path) -> dict | None:
    """Return the parsed self-pointer for *repo*, or None.

    Schema (§6.3): ``{schema_version, uid, name, real_path, registered_at, last_drift_check}``.
    The ``real_path`` field is the source of truth for "where this repo
    thinks it lives." A mismatch with the repo's actual current path means
    drift — see §7.2 for the handler.
    """
    return read_json_object(self_pointer_path(repo))


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
    return read_json_object(main_apiary_pointer_path(repo))


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
    return read_json_object(version_path(repo))


def write_version(repo: Path, payload: dict) -> Path:
    """Atomically write the version pin for *repo*."""
    payload = {"schema_version": PIN_SCHEMA_VERSION, **payload}
    return _write_json_file(version_path(repo), payload)


if __name__ == "__main__":
    # Print the per-target state dir for the cwd. Used by skill templates
    # and shell snippets that need the path without re-implementing the
    # registry lookup. Invoked via:
    #   python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" core/utils/state.py
    try:
        print(resolve_target_state_dir())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
