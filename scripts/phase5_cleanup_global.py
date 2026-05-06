#!/usr/bin/env python3
"""Phase-5 filesystem cleanup: remove every apiary-owned file under ``~/.claude/``.

Per MIGRATION-PLAN.md §10 phase 5 + §7.12.

This script handles the **file-deletion half** of phase 5. Run it only
after phase 4 has fully cut over (apiary hooks are gone from
``~/.claude/settings.json``) and every bootstrapped repo is confirmed
working under the per-repo install. Pre-phase-4 runs would break
sessions in the affected repos.

The **code-refactor half** of phase 5 — removing ``setup.py --global``,
the per-repo flag fallback in ``core/flags.py``, the
``APIARY_STATE_LAYOUT=legacy`` escape hatch, and doc references to the
global install — is intentionally NOT done here. It's a separate
multi-file edit that should land in its own commit once you've
confirmed the cleanup script left no surprises.

What this script removes (idempotent — missing items are skipped):

- ``~/.claude/apiary.json``                        (global pointer file)
- ``~/.claude/apiary_repos.json``                  (legacy registry)
- ``~/.claude/apiary_launch.py``                   (global launcher)
- ``~/.claude/apiary_bootstrap.py``                (installed copy of bootstrap CLI)
- ``~/.claude/.install-manifest.json``             (install hash manifest)
- ``~/.claude/apiary_gui/`` and ``apiary_gui_dev/``(GUI state — migrated by phase 3)
- ``~/.claude/{budgeter-log,budgeter-warn,budgeter-session-warn,auto-startup}-enabled``
- ``~/.claude/commands/<apiary-cmd>.md``           (16 known slash command names)
- ``~/.claude/.session-history.json``              (migrated by phase 3)
- ``~/.claude/.last-transcript.jsonl``             (working pointer)
- ``~/.claude/transcripts/``                       (migrated by phase 3)
- ``~/.claude/.session-identity-*``                (migrated by phase 3)

Apiary's hook entries in ``~/.claude/settings.json`` are NOT touched
here — phase 4 already strips them via ``hooks_lib.remove_hooks``. The
``~/.claude/CLAUDE.md`` zone is removed by phase 3
(``phase3_strip_global_zone.py``).

Default is dry-run. ``--apply`` writes. ``--skip-zone-check`` lets you
proceed even if ``~/.claude/CLAUDE.md`` still has the apiary zone (you
should run phase3_strip_global_zone.py first).

Usage::

    python scripts/phase5_cleanup_global.py             # dry-run
    python scripts/phase5_cleanup_global.py --apply
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import context_rules as cr

# Discrete files apiary installed under ~/.claude/.
_TOP_LEVEL_FILES = (
    "apiary.json",
    "apiary_repos.json",
    "apiary_launch.py",
    "apiary_bootstrap.py",
    ".install-manifest.json",
    ".session-history.json",
    ".last-transcript.jsonl",
)

# Toggle flag files. Mirror the list in core/flags.py / phase3_migrate_flags.
_FLAG_FILES = (
    "budgeter-log-enabled",
    "budgeter-warn-enabled",
    "budgeter-session-warn-enabled",
    "auto-startup-enabled",
)

# Directories apiary installed under ~/.claude/.
_TOP_LEVEL_DIRS = (
    "apiary_gui",
    "apiary_gui_dev",
    "transcripts",
)

# Slash command names apiary installs into ~/.claude/commands/.
_APIARY_SLASH_COMMANDS = (
    "apiary-context.md",
    "budgeter-log.md",
    "budgeter-session-warn.md",
    "budgeter-setup.md",
    "budgeter-warn.md",
    "compass-sync.md",
    "harden.md",
    "incubator.md",
    "note.md",
    "notes.md",
    "refine.md",
    "research.md",
    "review-learnings.md",
    "review.md",
    "runner-prep.md",
    "wrapup.md",
)

# Glob patterns for variable-name files.
_GLOB_PATTERNS = (
    ".session-identity-*.json",
)


def _global_dir(override: Path | None = None) -> Path:
    return override if override is not None else (Path.home() / ".claude")


def plan(global_dir: Path) -> list[tuple[str, Path]]:
    """Return ``[(label, path)]`` for every existing apiary-owned item.
    Items not present on disk are skipped (idempotent re-runs)."""
    items: list[tuple[str, Path]] = []

    for name in _TOP_LEVEL_FILES:
        p = global_dir / name
        if p.is_file():
            items.append(("file", p))

    for name in _FLAG_FILES:
        p = global_dir / name
        if p.is_file():
            items.append(("flag", p))

    for name in _TOP_LEVEL_DIRS:
        p = global_dir / name
        if p.is_dir():
            items.append(("dir", p))

    cmds = global_dir / "commands"
    if cmds.is_dir():
        for name in _APIARY_SLASH_COMMANDS:
            p = cmds / name
            if p.is_file():
                items.append(("command", p))

    for pattern in _GLOB_PATTERNS:
        for p in sorted(global_dir.glob(pattern)):
            if p.is_file():
                items.append(("glob", p))

    return items


def _zone_present(global_dir: Path) -> bool:
    """True if ``~/.claude/CLAUDE.md`` still has the apiary-managed zone.
    Phase-3 ``strip_global_zone.py`` should have removed it before phase 5
    runs; this is a safety check."""
    p = global_dir / "CLAUDE.md"
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        return cr.find_managed_zone(text) is not None
    except cr.ZoneTamperError:
        # Tampered: treat as "still present" so the operator deals with it.
        return True


def remove(items: list[tuple[str, Path]]) -> int:
    """Delete each path. Returns the number actually removed."""
    removed = 0
    for _label, p in items:
        if p.is_file():
            p.unlink()
            removed += 1
        elif p.is_dir():
            shutil.rmtree(p)
            removed += 1
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--global-dir", type=Path, default=None)
    parser.add_argument(
        "--skip-zone-check", action="store_true",
        help="proceed even if ~/.claude/CLAUDE.md still has the apiary zone",
    )
    args = parser.parse_args(argv)

    g = _global_dir(args.global_dir)

    if args.apply and not args.skip_zone_check and _zone_present(g):
        print(
            f"refusing to run: apiary zone still present in {g / 'CLAUDE.md'}.\n"
            "  Run scripts/phase3_strip_global_zone.py --apply first, "
            "or pass --skip-zone-check.", file=sys.stderr,
        )
        return 1

    items = plan(g)
    if not items:
        print(f"nothing to remove in {g} — already clean.")
        return 0

    print(f"planned removals ({len(items)} item(s)):")
    by_kind: dict[str, int] = {}
    for label, p in items:
        by_kind[label] = by_kind.get(label, 0) + 1
        print(f"  [{label}] {p}")
    summary = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
    print(f"\nsummary: {summary}")

    if not args.apply:
        print("\ndry-run; pass --apply to delete")
        return 0

    removed = remove(items)
    print(f"\napplied: deleted {removed} item(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
