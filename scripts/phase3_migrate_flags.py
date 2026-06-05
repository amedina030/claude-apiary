#!/usr/bin/env python3
"""Phase-3 migration: copy global flag files into every bootstrapped repo.

Per MIGRATION-PLAN.md §10 phase 3: read ``~/.claude/<flag>-enabled`` for
each apiary toggle flag; for every flag that's set globally, write the
same flag into every bootstrapped repo's
``<repo>/.claude/apiary/flags/<flag>-enabled``. After this runs, each
repo has its own copy of the user's previous global toggle state.

The fallback in ``core/flags.py`` (per-repo first, global second) keeps
old behavior working until phase 5 deletes the global files entirely;
this script's job is to make sure each repo's per-repo copy exists so
that phase 5 isn't a UX regression.

Idempotent. ``--apply`` writes; default is dry-run.

Usage::

    python scripts/phase3_migrate_flags.py             # dry-run
    python scripts/phase3_migrate_flags.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import state

# The set of flags apiary owns globally today. Sourced from
# MIGRATION-PLAN.md §3.5 D21 + §7.12. Other ``~/.claude/*-enabled``
# files are not apiary's to touch.
APIARY_FLAGS = (
    "budgeter-log",
    "budgeter-warn",
    "budgeter-session-warn",
    "auto-startup",
)


def _global_dir() -> Path:
    return Path.home() / ".claude"


def _per_repo_flags_dir(repo: Path) -> Path:
    return state.pin_dir(repo) / "flags"


def plan_copies(
    apiary: Path, *, global_dir: Path | None = None,
) -> list[tuple[int, str, str, Path]]:
    """Return ``[(uid, name, flag, dest)]`` for every (repo, flag) pair
    that needs a per-repo copy. ``dest`` is the file the copy would create.

    Skips:
    - flags not enabled globally (nothing to propagate),
    - repos whose ``real_path`` doesn't exist on disk,
    - repos lacking ``.claude/apiary/`` (not yet phase-2-bootstrapped),
    - destinations that already exist (idempotent re-runs are no-ops).
    """
    g = global_dir if global_dir is not None else _global_dir()
    enabled_globally = [f for f in APIARY_FLAGS if (g / f"{f}-enabled").is_file()]
    if not enabled_globally:
        return []

    registry = state._load_registry(apiary)
    plan: list[tuple[int, str, str, Path]] = []
    for uid_str, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        repo = Path(entry.get("real_path", ""))
        if not repo.is_dir():
            continue
        if not state.pin_dir(repo).is_dir():
            # Repo registered but not yet phase-1-bootstrapped per-repo.
            # Phase 2 reinstall will create the dir; this script can run
            # again afterwards.
            continue
        for flag in enabled_globally:
            dest = _per_repo_flags_dir(repo) / f"{flag}-enabled"
            if dest.is_file():
                continue
            plan.append((uid, entry.get("name", "?"), flag, dest))
    return plan


def apply_copies(plan: list[tuple[int, str, str, Path]]) -> int:
    """Write the per-repo flag files. Returns the number of files created."""
    written = 0
    for _uid, _name, _flag, dest in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("enabled", encoding="utf-8")
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="write changes (default: dry-run, prints planned copies only)",
    )
    parser.add_argument(
        "--apiary-repo", type=Path, default=None,
        help="path to main-apiary checkout (default: resolved via pointer)",
    )
    parser.add_argument(
        "--global-dir", type=Path, default=None,
        help="override the global flag directory (default: ~/.claude). "
             "Useful for tests.",
    )
    args = parser.parse_args(argv)

    apiary = state.resolve_apiary_repo(args.apiary_repo)
    plan = plan_copies(apiary, global_dir=args.global_dir)
    if not plan:
        print("nothing to migrate — no global flags set, or no eligible repos.")
        return 0

    print(f"planned copies ({len(plan)} file(s)):")
    for uid, name, flag, dest in plan:
        print(f"  [{uid}] {name}: {flag} -> {dest}")

    if not args.apply:
        print("\ndry-run; pass --apply to write changes")
        return 0

    written = apply_copies(plan)
    print(f"\napplied: wrote {written} flag file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
