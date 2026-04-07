#!/usr/bin/env python3
"""One-time migration: move scribe state from the legacy cwd-derived project key
to the stable key declared in .claude-project-key.

Only the scribe-owned files are moved:
    notes.jsonl, notes.jsonl.lock
    notes_archive.jsonl, notes_archive.jsonl.lock
    learnings.jsonl, learnings.jsonl.lock
    memory/  (entire directory)

Claude Code harness transcripts (<session_id>.jsonl and <session_id>/ dirs) are
deliberately left in place — they are owned by the harness, not by us, and
moving them would break the currently running session's transcript.

Idempotent:
    - If the legacy directory does not exist, exits with no action.
    - If the new key equals the legacy key, exits with no action.
    - If the destination already contains any owned file, refuses to clobber.

Usage:
    python scripts/migrate_project_key.py [--dry-run]
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.utils.project import get_project_key, project_key_from_path  # noqa: E402

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Files and directories owned by scribe that should migrate together.
OWNED_FILES = (
    "notes.jsonl",
    "notes.jsonl.lock",
    "notes_archive.jsonl",
    "notes_archive.jsonl.lock",
    "learnings.jsonl",
    "learnings.jsonl.lock",
)
OWNED_DIRS = ("memory",)


def _collect_present(src: Path) -> list[str]:
    """Return the owned files/dirs that actually exist under src."""
    present: list[str] = []
    for name in OWNED_FILES + OWNED_DIRS:
        if (src / name).exists():
            present.append(name)
    return present


def _destination_conflicts(dst: Path) -> list[str]:
    """Return owned entries that already exist at dst and would be clobbered."""
    if not dst.exists():
        return []
    return [name for name in OWNED_FILES + OWNED_DIRS if (dst / name).exists()]


def migrate(dry_run: bool = False) -> int:
    legacy_key = project_key_from_path(REPO_ROOT)
    new_key = get_project_key(REPO_ROOT)

    if new_key == legacy_key:
        print(f"no-op: stable key ({new_key}) already matches legacy key.")
        return 0

    src = PROJECTS_DIR / legacy_key
    dst = PROJECTS_DIR / new_key

    print(f"legacy key: {legacy_key}")
    print(f"stable key: {new_key}")
    print(f"source:     {src}")
    print(f"dest:       {dst}")

    if not src.exists():
        print("no-op: legacy directory does not exist; nothing to migrate.")
        return 0

    present = _collect_present(src)
    if not present:
        print("no-op: legacy directory has no scribe-owned files.")
        return 0

    conflicts = _destination_conflicts(dst)
    if conflicts:
        print(
            "refusing to clobber: destination already contains "
            f"{', '.join(conflicts)}. Resolve manually before re-running.",
            file=sys.stderr,
        )
        return 1

    print(f"will move: {', '.join(present)}")
    if dry_run:
        print("dry-run: no changes made.")
        return 0

    dst.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in present:
        src_path = src / name
        dst_path = dst / name
        shutil.move(str(src_path), str(dst_path))
        moved.append(name)

    print(f"moved {len(moved)} entries:")
    for name in moved:
        print(f"  → {name}")
    print("done. Scribe state now lives under the stable key.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would move without touching the filesystem.",
    )
    args = parser.parse_args()
    return migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
