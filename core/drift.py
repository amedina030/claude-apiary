"""Per-repo drift detection and handling for the per-repo install model.

Runs on every session open in a bootstrapped repo (called from a hook).
Verifies main-apiary is reachable, detects whether the repo has been
moved or copied since last bootstrap, and queues a mailbox message to
main-apiary so the registry can catch up.

See MIGRATION-PLAN.md §7.1 (verify), §7.2 (drift handling), §3.10
(copy detection).
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import cascade, mailbox
from core.utils import state
from core.utils.filelock import FileLock

# Main-apiary's UID is reserved at slot 1 by convention (§13.1, D3).
_MAIN_APIARY_UID = 1


@dataclasses.dataclass
class DriftReport:
    """Result of a drift check. ``action`` is one of:

    - ``"none"`` — no drift detected; self-pointer's last_drift_check
      was refreshed.
    - ``"move"`` — repo moved; an ``update_path`` mailbox message was
      queued.
    - ``"copy"`` — repo is a copy of an existing registered repo; a new
      uid was allocated and a ``register_copy`` mailbox message queued.
    - ``"skip"`` — main-apiary is unreachable or self-pointer is missing;
      no mailbox write happened. ``message`` describes what to tell the
      user.
    - ``"not_bootstrapped"`` — pin files don't exist at this repo;
      apiary install was never run here.
    """
    action: str
    message: str = ""
    old_uid: int | None = None
    new_uid: int | None = None


def check_and_handle(repo_root: Path) -> DriftReport:
    """Top-level entry point — runs every session for a bootstrapped repo.

    Steps (per §7.1 + §7.2):

    1. Read self-pointer; if missing, this repo was never bootstrapped.
    2. Read main-apiary-pointer; verify the path exists and is a real
       main-apiary checkout (its own self-pointer is present and aligned
       with its actual path).
    3. Compute drift = ``self.real_path != repo_root``.
    4. No drift → refresh ``last_drift_check`` and return ``none``.
    5. Drift → classify move-vs-copy, write mailbox message, update local
       self-pointer, return the appropriate action.
    """
    repo_root = Path(repo_root).resolve()
    self_p = state.read_self_pointer(repo_root)
    if self_p is None:
        return DriftReport(
            action="not_bootstrapped",
            message=f"{repo_root} has no apiary self-pointer; not a bootstrapped repo",
        )

    # Main-apiary itself takes the cascade-fix path (§7.3) instead of the
    # mailbox path. Detect by reserved uid=1 (D3).
    if int(self_p.get("uid", -1)) == _MAIN_APIARY_UID:
        return _handle_main_apiary_drift(repo_root, self_p)

    main_p = state.read_main_apiary_pointer(repo_root)
    if main_p is None:
        return DriftReport(
            action="skip",
            message=f"main-apiary-pointer missing at {state.main_apiary_pointer_path(repo_root)}; "
                    "running as vanilla session.",
        )

    apiary_path = Path(main_p.get("main_apiary_path", ""))
    skip_msg = _verify_main_apiary(apiary_path)
    if skip_msg is not None:
        return DriftReport(action="skip", message=skip_msg)

    recorded = Path(self_p.get("real_path", "")).resolve() if self_p.get("real_path") else None
    drifted = recorded != repo_root

    if not drifted:
        # Refresh last_drift_check and exit fast.
        self_p["last_drift_check"] = state._now_iso()
        state.write_self_pointer(repo_root, self_p)
        return DriftReport(action="none")

    return _handle_drift(repo_root, apiary_path, self_p)


def _verify_main_apiary(apiary_path: Path) -> str | None:
    """Per §7.1. Returns a skip message if main-apiary is unreachable,
    or None if everything checks out."""
    if not apiary_path or not apiary_path.is_dir():
        return (f"[apiary] main checkout not found at {apiary_path}; "
                "running as vanilla session.")
    # Main-apiary must itself be bootstrapped (have its own self-pointer).
    main_self_path = state.self_pointer_path(apiary_path)
    if not main_self_path.is_file():
        return (f"[apiary] {apiary_path} is not a valid main-apiary checkout "
                f"(no {main_self_path.name}); running as vanilla session.")
    main_self = state.read_self_pointer(apiary_path)
    if main_self is None:
        return (f"[apiary] {apiary_path}/.claude/apiary/self-pointer.json malformed; "
                "running as vanilla session.")
    main_recorded = Path(main_self.get("real_path", "")).resolve()
    main_actual = apiary_path.resolve()
    if main_recorded != main_actual:
        return (f"[apiary] main-apiary self-pointer out of sync: "
                f"recorded={main_recorded} actual={main_actual}. "
                f"Run `apiary doctor pointers` from {main_actual}.")
    return None


def _handle_drift(
    repo_root: Path, apiary_path: Path, self_p: dict,
) -> DriftReport:
    """Per §7.2 + §3.10 — classify move vs copy, write mailbox message."""
    uid = int(self_p["uid"])
    name = self_p["name"]
    version = state.read_apiary_version(apiary_path)
    now = state._now_iso()

    # Read registry under FileLock to make the copy classification atomic
    # with respect to other allocators.
    with FileLock(state.registry_path(apiary_path)):
        registry = state._load_registry(apiary_path)
        entry = registry.get(str(uid))

        is_copy = _classify_as_copy(entry, repo_root, uid)

        if is_copy:
            new_uid = state.allocate_next_id(apiary_path)
            # Overwrite our self-pointer with the new uid; this happens
            # before the mailbox write so that even if the mailbox write
            # fails, we don't reuse the original uid.
            new_self = {
                "uid": new_uid,
                "name": name,
                "real_path": str(repo_root),
                "registered_at": now,
                "last_drift_check": now,
            }
            state.write_self_pointer(repo_root, new_self)
            mailbox.write_message(
                apiary_path,
                from_uid=new_uid,
                kind=mailbox.KIND_REGISTER_COPY,
                new_path=repo_root,
                name=name,
                version=version,
            )
            return DriftReport(
                action="copy", old_uid=uid, new_uid=new_uid,
                message=f"detected copy of repo uid={uid}; registered as new uid={new_uid}",
            )
        else:
            # MOVE scenario — same uid, new path.
            old_path = entry.get("real_path") if entry else None
            self_p["real_path"] = str(repo_root)
            self_p["last_drift_check"] = now
            state.write_self_pointer(repo_root, self_p)
            mailbox.write_message(
                apiary_path,
                from_uid=uid,
                kind=mailbox.KIND_UPDATE_PATH,
                new_path=repo_root,
                old_path=old_path,
                name=name,
                version=version,
            )
            return DriftReport(
                action="move", old_uid=uid, new_uid=uid,
                message=f"detected move; queued registry update for uid={uid}",
            )


def _handle_main_apiary_drift(repo_root: Path, self_p: dict) -> DriftReport:
    """Per §7.3: main-apiary's drift handler updates its own self-pointer
    and main-apiary-pointer, then cascade-fixes every other bootstrapped
    repo's ``main-apiary-pointer.json`` to the new location."""
    recorded = Path(self_p.get("real_path", "")).resolve() if self_p.get("real_path") else None
    if recorded == repo_root:
        # No drift — refresh check timestamp and return.
        self_p["last_drift_check"] = state._now_iso()
        state.write_self_pointer(repo_root, self_p)
        return DriftReport(action="none")

    # Main-apiary moved. Update its own pin files first so the cascade
    # writes valid paths into bootstrapped repos.
    now = state._now_iso()
    self_p["real_path"] = str(repo_root)
    self_p["last_drift_check"] = now
    state.write_self_pointer(repo_root, self_p)
    state.write_main_apiary_pointer(repo_root, {
        "main_apiary_path": str(repo_root),
        "main_apiary_uid": _MAIN_APIARY_UID,
        "registered_at": now,
    })

    # Update its own registry entry (real_path moves).
    with FileLock(state.registry_path(repo_root)):
        registry = state._load_registry(repo_root)
        if str(_MAIN_APIARY_UID) in registry:
            registry[str(_MAIN_APIARY_UID)]["real_path"] = str(repo_root)
            registry[str(_MAIN_APIARY_UID)]["last_used"] = now
            state._save_registry(repo_root, registry)

    cascade_report = cascade.cascade_fix(repo_root)
    n = len(cascade_report.updated)
    msg = f"main-apiary moved to {repo_root}; cascade-fix updated {n} bootstrapped repo(s)"
    if cascade_report.skipped:
        msg += f" ({len(cascade_report.skipped)} skipped)"
    return DriftReport(action="move", old_uid=_MAIN_APIARY_UID, new_uid=_MAIN_APIARY_UID, message=msg)


def _classify_as_copy(entry: dict | None, repo_root: Path, uid: int) -> bool:
    """Per §3.10 step (b)+(c): we're a copy iff the registry's entry for
    our uid still points at a real directory and that directory's
    self-pointer claims the same uid we do. Otherwise we're the original
    that just moved."""
    if entry is None:
        return False
    other_path = entry.get("real_path", "")
    if not other_path:
        return False
    other = Path(other_path)
    if not other.is_dir() or other.resolve() == repo_root:
        return False
    other_self = state.read_self_pointer(other)
    if other_self is None:
        return False
    return int(other_self.get("uid", -1)) == uid
