"""Cascade-fix — when main-apiary moves, rewrite every bootstrapped repo's
``main-apiary-pointer.json`` to the new location.

Main-apiary's drift handler is the only code path that writes into
other bootstrapped repos' files (see
``docs/architecture/per-repo-install.md``). Every other code
path is read-only with respect to bootstrapped repos. The cascade closes
the loop when main-apiary itself moves — without it, bootstrapped repos
would point at a stale path forever.

Invoked from:

- Main-apiary's drift handler (when it detects self-pointer drift on its
  own startup).
- ``apiary doctor pointers --fix`` (manual operator trigger).
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.utils import state
from core.utils.filelock import FileLock


@dataclasses.dataclass
class CascadeReport:
    """Summary of a cascade-fix run."""
    new_main_apiary_path: Path
    updated: list[int]    # uids whose main-apiary-pointer was rewritten
    skipped: list[tuple[int, str]]  # (uid, reason) — repo gone, no pin file, etc.


def cascade_fix(new_main_apiary_path: Path) -> CascadeReport:
    """Rewrite every bootstrapped repo's ``main-apiary-pointer.json`` to
    point at *new_main_apiary_path*.

    Skips main-apiary's own entry (``state.MAIN_APIARY_UID``) and any
    registry entry whose
    ``real_path`` no longer exists or no longer has a per-repo pin file.
    Skipped entries are returned in the report so the caller can surface
    them; ``apiary doctor unreachable`` covers persistent gone-repo state.
    """
    apiary = Path(new_main_apiary_path).resolve()
    report = CascadeReport(new_main_apiary_path=apiary, updated=[], skipped=[])

    with FileLock(state.registry_path(apiary)):
        registry = state._load_registry(apiary)
        for uid_str, entry in registry.items():
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            if uid == state.MAIN_APIARY_UID:
                continue  # main-apiary's own entry; updated by the caller
            real_path = entry.get("real_path", "")
            if not real_path:
                report.skipped.append((uid, "no real_path"))
                continue
            repo = Path(real_path)
            if not repo.is_dir():
                report.skipped.append((uid, f"path missing: {real_path}"))
                continue
            pin_path = state.main_apiary_pointer_path(repo)
            if not pin_path.is_file():
                report.skipped.append((uid, f"no main-apiary-pointer at {pin_path}"))
                continue
            existing = state.read_main_apiary_pointer(repo) or {}
            existing["main_apiary_path"] = str(apiary)
            state.write_main_apiary_pointer(repo, existing)
            report.updated.append(uid)

    return report
