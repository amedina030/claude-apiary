#!/usr/bin/env python3
"""Phase-3 migration: copy ``~/.claude/apiary_gui/`` and
``~/.claude/apiary_gui_dev/`` into ``<main-apiary>/.apiary/gui/``.

Per MIGRATION-PLAN.md §10 phase 3 + §3.6 D22:

State files (tabs.json, sidebar_state.json, theme.json, launch.json,
composer_state.json, captures/, permission_mcp_*) move from the global
location to ``<main-apiary>/.apiary/gui/``. The GUI source code under
``<main-apiary>/gui/`` is unchanged; only state moves.

Per §14.3: the GUI must NOT be running during this migration — file
state could be in flight. The script refuses to run if it detects
recent activity in ``permission_mcp.log`` (within the last 30 seconds)
unless ``--force`` is passed. Pass ``--no-active-check`` to skip the
check entirely (e.g. when the log itself is gone).

Idempotent. ``--apply`` writes; default is dry-run.
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

_GUI_DIRS = ("apiary_gui", "apiary_gui_dev")
# Recent-activity threshold for the permission_mcp.log freshness check.
_ACTIVITY_THRESHOLD_SECONDS = 30


def _global_dir(override: Path | None = None) -> Path:
    return override if override is not None else (Path.home() / ".claude")


def _gui_state_dir(apiary: Path) -> Path:
    return Path(apiary) / ".apiary" / "gui"


def detect_active_gui(global_dir: Path) -> tuple[bool, str]:
    """Return (active, reason). Looks at permission_mcp.log mtime."""
    log = global_dir / "apiary_gui" / "permission_mcp.log"
    if not log.is_file():
        return False, "no log file"
    age = time.time() - log.stat().st_mtime
    if age < _ACTIVITY_THRESHOLD_SECONDS:
        return True, f"log modified {age:.1f}s ago (< {_ACTIVITY_THRESHOLD_SECONDS}s threshold)"
    return False, f"log last modified {age:.1f}s ago"


def plan_copies(
    apiary: Path, *, global_dir: Path | None = None,
) -> list[tuple[str, Path, Path]]:
    """Return ``[(label, src, dest)]`` for every file/dir to migrate.

    Files are copied (not moved) so the global location remains intact
    until phase-5 cleanup runs. Directories like ``captures/`` are
    recursively merged.
    """
    g = _global_dir(global_dir)
    target = _gui_state_dir(apiary)
    plan: list[tuple[str, Path, Path]] = []
    for src_name in _GUI_DIRS:
        src = g / src_name
        if not src.is_dir():
            continue
        # Each top-level entry under apiary_gui[_dev] becomes its own item.
        for child in sorted(src.iterdir()):
            dest = target / src_name / child.name
            if dest.exists():
                # Skip identical files; let captures/ merge separately
                continue
            label = f"{src_name}/{child.name}"
            plan.append((label, child, dest))
    return plan


def apply_copies(plan: list[tuple[str, Path, Path]]) -> int:
    """Perform the file/dir copies."""
    written = 0
    for _label, src, dest in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apiary-repo", type=Path, default=None)
    parser.add_argument("--global-dir", type=Path, default=None)
    parser.add_argument(
        "--force", action="store_true",
        help="proceed even if the GUI looks active",
    )
    parser.add_argument(
        "--no-active-check", action="store_true",
        help="skip the permission_mcp.log freshness check entirely",
    )
    args = parser.parse_args(argv)

    apiary = state.resolve_apiary_repo(args.apiary_repo).resolve()
    g = _global_dir(args.global_dir)

    if not args.no_active_check and args.apply:
        active, reason = detect_active_gui(g)
        if active and not args.force:
            print(f"refusing to migrate: GUI looks active ({reason}). "
                  "Stop the GUI process or pass --force.", file=sys.stderr)
            return 1

    plan = plan_copies(apiary, global_dir=args.global_dir)
    if not plan:
        print(f"no GUI state to migrate from {g}.")
        return 0

    print(f"planned copies ({len(plan)} item(s)):")
    for label, src, dest in plan:
        print(f"  {label}: {src} -> {dest}")

    if not args.apply:
        print("\ndry-run; pass --apply to write changes")
        return 0

    written = apply_copies(plan)
    print(f"\napplied: copied {written} item(s) into {_gui_state_dir(apiary)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
