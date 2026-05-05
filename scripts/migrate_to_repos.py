#!/usr/bin/env python3
"""One-shot migration: <target>/.apiary/ → <apiary>/.repos/<name>-<id>/.

Moves a target repo's per-tool state out of its in-repo ``.apiary/``
directory into the centralized ``.repos/`` registry under the apiary
checkout. Leaves a renamed snapshot at ``<target>/.apiary.pre-migration/``
and writes a fresh ``<target>/.apiary/pointer`` JSON for bidirectional
discovery.

Usage::

    python scripts/migrate_to_repos.py --target /abs/path/to/target_repo
    python scripts/migrate_to_repos.py --target /abs/path/to/target_repo --dry-run

Idempotency check: presence of ``<target>/.apiary.pre-migration/`` means
the migration already ran — script errors out without touching anything.
A bare pointer file at ``<target>/.apiary/pointer`` is NOT a migration
marker on its own (lazy auto-registration writes pointers too).

Spec: scribe note C-2026-46.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import state

POINTER_DIRNAME = ".apiary"
POINTER_FILENAME = "pointer"
PRE_MIGRATION_DIRNAME = ".apiary.pre-migration"

# Names inside <target>/.apiary/ that should NOT be carried into the
# centralized state dir. The pointer is a per-target breadcrumb the
# resolver rewrites on first call; everything else under .apiary/ is
# state we want to preserve.
_SKIP_NAMES = frozenset({POINTER_FILENAME})


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def migrate(target: Path, *, dry_run: bool = False) -> int:
    target = Path(target).resolve()
    if not target.is_dir():
        return _err(f"target path is not a directory: {target}")

    apiary_dir = target / POINTER_DIRNAME
    pre_dir = target / PRE_MIGRATION_DIRNAME

    if pre_dir.exists():
        return _err(
            f"already migrated: {pre_dir} exists. "
            f"Remove it manually if you intend to re-migrate."
        )

    if not apiary_dir.is_dir():
        return _err(f"no .apiary/ directory at {apiary_dir} — nothing to migrate")

    # Items to move (skip pointer; everything else is state we preserve).
    items = [p for p in apiary_dir.iterdir() if p.name not in _SKIP_NAMES]
    if not items:
        return _err(
            f"{apiary_dir} has nothing to migrate (only the pointer or empty)"
        )

    # Resolve / register the target. This creates the .repos/<name>-<id>/
    # entry if it doesn't exist yet, or returns the existing one.
    apiary_repo = state.resolve_apiary_repo()
    state_dir = state.resolve_target_state_dir(cwd=target, apiary_repo=apiary_repo)

    # Detect resume-after-partial-failure: if every item we'd copy is
    # already present in state_dir (matching name set), the copy step
    # already succeeded on a prior run that died before the rename. Skip
    # the copy and proceed straight to rename + pointer.
    src_names = {p.name for p in items}
    existing_names = {p.name for p in state_dir.iterdir() if p.name not in _SKIP_NAMES}
    resume_mode = bool(existing_names) and src_names.issubset(existing_names)
    if existing_names and not resume_mode:
        return _err(
            f"centralized state dir is not empty: {state_dir}. "
            f"Contains: {sorted(existing_names)}. "
            f"Resolve manually before re-running migration."
        )

    print(f"target:  {target}")
    print(f"apiary:  {apiary_repo}")
    print(f"state:   {state_dir}")
    print(f"items:   {[p.name for p in items]}")
    if resume_mode:
        print(f"(resume mode — copy already complete in {state_dir})")
    if dry_run:
        print("(dry-run — no files moved)")
        return 0

    if not resume_mode:
        # Copy each top-level item into the state dir. We use copytree/copy2
        # rather than rename because the source and destination may live on
        # different volumes (Windows D:\ vs C:\Users\...), and rename across
        # devices fails.
        for item in items:
            dest = state_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    # Snapshot the original .apiary/ as .apiary.pre-migration/.
    #
    # First try a parent-directory rename — fastest, single atomic syscall.
    # On Windows, copytree's file handles can take a few hundred ms to
    # release after the copy returns, so a same-instant rename of the
    # parent directory may hit ERROR_ACCESS_DENIED — retry with backoff.
    #
    # If even retries fail, fall back to: mkdir .apiary.pre-migration/
    # and Move-Item each child individually. This works even when
    # something holds a directory-level handle on .apiary/ itself
    # (orphan tail processes, Windows dir watchers, indexer hooks) but
    # not on its children. End state is identical to the rename path.
    last_err: Exception | None = None
    for delay in (0, 0.25, 0.5, 1.0, 2.0):
        if delay:
            time.sleep(delay)
        try:
            apiary_dir.rename(pre_dir)
            last_err = None
            break
        except (OSError, PermissionError) as exc:
            last_err = exc
    if last_err is not None:
        try:
            pre_dir.mkdir(parents=True, exist_ok=False)
            for child in list(apiary_dir.iterdir()):
                child.rename(pre_dir / child.name)
            print(
                f"(parent-rename blocked; moved {len(list(pre_dir.iterdir()))} "
                f"children individually instead)"
            )
        except OSError as exc2:
            return _err(
                f"could not snapshot {apiary_dir}: parent rename failed "
                f"({last_err}); per-child move also failed ({exc2}). "
                f"State was copied to {state_dir}; resolve manually and re-run."
            )
    else:
        # Parent rename succeeded — recreate the empty .apiary/ for the
        # fresh pointer file we're about to write.
        apiary_dir.mkdir(parents=True, exist_ok=False)

    # Write a fresh .apiary/pointer reflecting the centralized layout.
    state._write_pointer(target, apiary_repo, state_dir.name)

    print(f"migrated {len(items)} item(s) into {state_dir}")
    print(f"original snapshot at {pre_dir}")
    print(f"pointer written at {apiary_dir / POINTER_FILENAME}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target", required=True, type=Path,
        help="Absolute path to the target repo to migrate",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without moving any files",
    )
    args = parser.parse_args(argv)
    return migrate(args.target, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
