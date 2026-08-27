#!/usr/bin/env python3
"""Backup scribe indexes to backups/<YYYY-MM-DD>/ with retention pruning."""

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scribe.store import TYPE_FOLDERS, LEARNING_FOLDER, INDEX_FILENAME, ARCHIVE_DIRNAME
from scribe.notes import scribe_state_dir, PROJECTS_DIR, _project_key

BACKUPS_DIRNAME = 'backups'
DEFAULT_RETAIN = 30


def resolve_backup_source(project: str | None = None) -> Path:
    """The scribe state dir to back up, with the legacy per-project fallback.

    Not ``core.utils.state.resolve_state_dir`` — this one adds the
    ``~/.claude/projects/<key>/`` fallback that only the backup CLI still
    honours, and it is named apart so there is exactly one
    ``resolve_state_dir`` in the tree (review X-3).
    """
    sd = scribe_state_dir()
    if sd is None:
        sd = PROJECTS_DIR / _project_key(project)
    return Path(sd)


def create_backup(state_dir: Path, backups_root: Path, date_str: str) -> tuple[Path, int]:
    target = backups_root / date_str
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
        type_dir = state_dir / folder_name
        if not type_dir.exists():
            continue
        for year_dir in type_dir.iterdir():
            if not (year_dir.is_dir() and year_dir.name.isdigit() and len(year_dir.name) == 4):
                continue
            for rel in ((INDEX_FILENAME,), (ARCHIVE_DIRNAME, INDEX_FILENAME)):
                src = year_dir.joinpath(*rel)
                if src.exists():
                    dst = target / folder_name / year_dir.name
                    for part in rel[:-1]:
                        dst = dst / part
                    dst = dst / rel[-1]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    count += 1
    return (target, count)


def prune_backups(backups_root: Path, retain: int) -> list[Path]:
    if not backups_root.exists():
        return []
    all_dirs = sorted(
        [d for d in backups_root.iterdir() if d.is_dir()],
        key=lambda p: p.name,
    )
    if retain == 0:
        to_delete = all_dirs[:-1] if len(all_dirs) > 1 else []
    else:
        to_delete = all_dirs[:-retain] if len(all_dirs) > retain else []
    for d in to_delete:
        shutil.rmtree(d)
    return to_delete


def main() -> int:
    parser = argparse.ArgumentParser(description='Backup scribe indexes with retention pruning.')
    parser.add_argument('--retain', type=int, default=DEFAULT_RETAIN,
                        help='Number of backup dirs to keep (default 30)')
    parser.add_argument('--project', default=None, help='Project key override')
    args = parser.parse_args()

    state_dir = resolve_backup_source(args.project)
    if not state_dir.exists():
        print('No scribe state directory found.', file=sys.stderr)
        return 0

    backups_root = state_dir / BACKUPS_DIRNAME
    backups_root.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    target, count = create_backup(state_dir, backups_root, date_str)
    print(f'Backup created: {target} ({count} files)')

    pruned = prune_backups(backups_root, args.retain)
    if pruned:
        print(f'Pruned {len(pruned)} old backup(s)')

    return 0


if __name__ == '__main__':
    sys.exit(main())
