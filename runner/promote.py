#!/usr/bin/env python3
"""Promote a backlog ticket to runner intake.

Validates, assigns a UUID, copies to intake/, removes the backlog file,
and updates board.md status from 'backlog' to 'ready'.

Usage:
    promote.py <slug>
"""
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKLOG_DIR = SCRIPT_DIR / "backlog"
INTAKE_DIR = SCRIPT_DIR / "intake"
BOARD_PATH = SCRIPT_DIR / "board.md"
VALIDATE_SCRIPT = SCRIPT_DIR / "validate_intake.py"


def update_board_row(slug: str, new_status: str, intake_uuid: str) -> None:
    if not BOARD_PATH.exists():
        return
    text = BOARD_PATH.read_text(encoding='utf-8')
    lines = text.splitlines(keepends=True)
    updated = []
    for line in lines:
        cols = line.split('|')
        # Exact slug column match (col index 1) to avoid prefix collisions
        if len(cols) >= 6 and cols[1].strip() == slug:
            title = cols[2].strip()
            line = f'| {slug} | {title} | {new_status} | {intake_uuid} | |\n'
        updated.append(line)
    BOARD_PATH.write_text(''.join(updated), encoding='utf-8')


def main():
    if len(sys.argv) < 2:
        print('Usage: promote.py <slug>', file=sys.stderr)
        sys.exit(1)

    slug = sys.argv[1]
    # Prevent path traversal: slug must be a plain filename with no separators
    if (
        '/' in slug
        or '\\' in slug
        or '\x00' in slug
        or slug in ('.', '..')
        or Path(slug) != Path(Path(slug).name)
        or not Path(slug).name
    ):
        print('Error: invalid slug (path separators not allowed)', file=sys.stderr)
        sys.exit(1)
    backlog_path = BACKLOG_DIR / f'{slug}.json'

    if not backlog_path.exists():
        print(f'Error: backlog ticket {slug}.json not found', file=sys.stderr)
        sys.exit(1)

    data = json.loads(backlog_path.read_text(encoding='utf-8'))
    required_keys = ['title', 'problem', 'description', 'scope', 'created_at']
    missing_keys = [k for k in required_keys if k not in data]
    if missing_keys:
        print(f"Error: backlog ticket is missing required fields: {', '.join(missing_keys)}", file=sys.stderr)
        sys.exit(1)
    intake_id = str(uuid.uuid4())

    intake = {
        'id': intake_id,
        'title': data['title'],
        'problem': data['problem'],
        'description': data['description'],
        'scope': data['scope'],
        'context': data.get('context', ''),
        'created_at': data['created_at'],
    }
    if 'source' in data:
        intake['source'] = data['source']

    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    intake_path = INTAKE_DIR / f'{intake_id}.json'
    intake_path.write_text(json.dumps(intake, indent=2), encoding='utf-8')

    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(intake_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        intake_path.unlink(missing_ok=True)
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(1)

    backlog_path.unlink()
    update_board_row(slug, 'ready', intake_id)
    print(str(intake_path))
    print(intake_id)


if __name__ == '__main__':
    main()
