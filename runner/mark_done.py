#!/usr/bin/env python3
"""Mark a backlog ticket as done without running it through the runner.

For tickets small enough to fix by hand. Deletes runner/backlog/<slug>.json
and updates the matching runner/board.md row's status column to 'done'.
Refuses to operate if the row is not currently in 'backlog' status, to avoid
clobbering runner-run state.

Usage:
    mark_done.py <slug> [--note "explanation"]
"""
import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKLOG_DIR = SCRIPT_DIR / "backlog"
BOARD_PATH = SCRIPT_DIR / "board.md"


def _slug_is_safe(slug: str) -> bool:
    if (
        '/' in slug
        or '\\' in slug
        or '\x00' in slug
        or slug in ('.', '..')
        or Path(slug) != Path(Path(slug).name)
        or not Path(slug).name
    ):
        return False
    return True


def _find_row_status(slug: str) -> str | None:
    """Return the status column for the row matching slug, or None if not found."""
    if not BOARD_PATH.exists():
        return None
    text = BOARD_PATH.read_text(encoding='utf-8')
    for line in text.splitlines():
        cols = line.split('|')
        if len(cols) >= 6 and cols[1].strip() == slug:
            return cols[3].strip()
    return None


def _rewrite_board_row(slug: str, new_status: str, note: str) -> bool:
    """Rewrite the row matching slug. Returns True if a row was updated."""
    text = BOARD_PATH.read_text(encoding='utf-8')
    lines = text.splitlines(keepends=True)
    updated = False
    out = []
    for line in lines:
        cols = line.split('|')
        if len(cols) >= 6 and cols[1].strip() == slug:
            title = cols[2].strip()
            uuid_col = cols[4].strip()
            # Preserve trailing newline if present
            newline = '\n' if line.endswith('\n') else ''
            line = f'| {slug} | {title} | {new_status} | {uuid_col} | {note} |{newline}'
            updated = True
        out.append(line)
    if updated:
        BOARD_PATH.write_text(''.join(out), encoding='utf-8')
    return updated


def main():
    parser = argparse.ArgumentParser(description="Mark a backlog ticket as done.")
    parser.add_argument('slug', help='Backlog ticket slug (filename without .json)')
    parser.add_argument('--note', default='', help='Optional note describing the manual completion')
    args = parser.parse_args()

    slug = args.slug
    if not _slug_is_safe(slug):
        print('Error: invalid slug (path separators not allowed)', file=sys.stderr)
        sys.exit(1)

    backlog_path = BACKLOG_DIR / f'{slug}.json'
    if not backlog_path.exists():
        print(f'Error: backlog ticket {slug}.json not found', file=sys.stderr)
        sys.exit(1)

    current_status = _find_row_status(slug)
    if current_status is None:
        print(f'Error: no board.md row found for slug {slug}', file=sys.stderr)
        sys.exit(1)
    if current_status != 'backlog':
        print(
            f"Error: refusing to mark done — board row for {slug} is in '{current_status}' status, "
            f"not 'backlog'. mark_done is only for tickets that never entered the runner.",
            file=sys.stderr,
        )
        sys.exit(1)

    note = args.note.strip()
    if not _rewrite_board_row(slug, 'done', note):
        print(f'Error: failed to rewrite board.md row for {slug}', file=sys.stderr)
        sys.exit(1)

    backlog_path.unlink()
    print(f'Marked {slug} as done.')


if __name__ == '__main__':
    main()
