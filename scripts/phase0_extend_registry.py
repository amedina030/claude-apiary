#!/usr/bin/env python3
"""One-shot phase-0 migration: backfill ``uid`` and ``version`` fields onto
existing entries in ``<main-apiary>/.repos/registry.json``.

Per MIGRATION-PLAN.md §6.5 + phase 0 (line 913 of the plan), every registry
entry post-migration carries:

- ``uid`` — same as the registry key, but as an int (intentional duplication
  for clarity in downstream code that walks values rather than keys).
- ``version`` — the apiary version this repo was last bootstrapped or updated
  to. For phase-0 backfill, set to ``<main-apiary>/VERSION`` (currently 0.1.0)
  for all existing entries.

Idempotent: re-running on an already-extended registry leaves it untouched.

Usage::

    python scripts/phase0_extend_registry.py            # dry-run report
    python scripts/phase0_extend_registry.py --apply    # write changes
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import state
from core.utils.filelock import FileLock


def _plan_changes(registry: dict, fallback_version: str) -> list[tuple[str, dict]]:
    """Return a list of (id_str, fields_to_add) for entries needing backfill.
    An empty list means the registry is already at the new schema."""
    changes: list[tuple[str, dict]] = []
    for id_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        try:
            uid_int = int(id_str)
        except ValueError:
            print(f"warn: registry key {id_str!r} is not an integer; skipping", file=sys.stderr)
            continue
        to_add: dict = {}
        if "uid" not in entry:
            to_add["uid"] = uid_int
        elif entry["uid"] != uid_int:
            print(
                f"warn: registry[{id_str}].uid={entry['uid']} disagrees with key "
                f"({uid_int}); leaving as-is — investigate manually",
                file=sys.stderr,
            )
        if "version" not in entry:
            to_add["version"] = fallback_version
        if to_add:
            changes.append((id_str, to_add))
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="write changes (default: dry-run, prints planned changes only)",
    )
    parser.add_argument(
        "--apiary-repo", type=Path, default=PROJECT_ROOT,
        help="path to main-apiary checkout (default: this script's repo)",
    )
    args = parser.parse_args()

    apiary = args.apiary_repo.resolve()
    registry_p = state.registry_path(apiary)
    if not registry_p.is_file():
        print(f"error: registry not found at {registry_p}", file=sys.stderr)
        return 1

    fallback_version = state.read_apiary_version(apiary)

    with FileLock(registry_p):
        registry = state._load_registry(apiary)
        changes = _plan_changes(registry, fallback_version)

        if not changes:
            print("registry already at new schema — nothing to do")
            return 0

        print(f"planned changes ({len(changes)} entries):")
        for id_str, to_add in changes:
            entry = registry[id_str]
            name = entry.get("name", "?")
            adds = ", ".join(f"{k}={v!r}" for k, v in to_add.items())
            print(f"  [{id_str}] {name}: add {adds}")

        if not args.apply:
            print("\ndry-run; pass --apply to write changes")
            return 0

        for id_str, to_add in changes:
            registry[id_str].update(to_add)
        state._save_registry(apiary, registry)
        print(f"\napplied: {registry_p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
