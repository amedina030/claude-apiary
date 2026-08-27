#!/usr/bin/env python3
"""Backup scribe indexes to backups/<YYYY-MM-DD>/ with retention pruning.

The same operation as ``scribe/notes.py backup``, kept because it is what
anything scheduling a snapshot (a cron entry, a task) already invokes. The
copying and pruning live in :mod:`scribe.maintenance` — this file is the
older entry point, not a second implementation.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scribe.maintenance import (
    BACKUPS_DIRNAME, DEFAULT_RETAIN, create_backup, prune_backups,
)
from scribe.notes import scribe_state_dir, PROJECTS_DIR, _project_key


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


def main() -> int:
    parser = argparse.ArgumentParser(description='Backup scribe indexes with retention pruning.')
    parser.add_argument('--retain', type=int, default=DEFAULT_RETAIN,
                        help=f'Number of backup dirs to keep (default {DEFAULT_RETAIN})')
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
