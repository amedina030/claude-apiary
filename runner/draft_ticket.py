#!/usr/bin/env python3
"""Create a backlog draft ticket for the runner.

Writes a JSON file to runner/backlog/<slug>.json.

Usage:
    draft_ticket.py --title '...' --problem '...' --description '...' --scope '...'
    draft_ticket.py --from-todo <id> --title '...' --scope '...'
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKLOG_DIR = SCRIPT_DIR / "backlog"
NOTES_SCRIPT = SCRIPT_DIR.parent / "scribe" / "notes.py"


def slugify(title: str) -> str:
    lowered = title.lower()
    result = re.sub(r'[^a-z0-9-]', '-', lowered)
    result = re.sub(r'-+', '-', result)
    result = result.strip('-')
    result = result[:60]
    result = result.strip('-')
    return result


def read_todo(todo_id: str) -> str:
    result = subprocess.run(
        [sys.executable, str(NOTES_SCRIPT), 'get', str(todo_id)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f'Error: note {todo_id} not found', file=sys.stderr)
        sys.exit(1)

    lines = result.stdout.splitlines()
    try:
        sep_index = lines.index('---')
        content = '\n'.join(lines[sep_index + 1:]).strip()
    except ValueError:
        content = result.stdout.strip()

    if not content:
        print(f'Error: note {todo_id} not found', file=sys.stderr)
        sys.exit(1)

    return content


def main():
    parser = argparse.ArgumentParser(description='Create a backlog draft ticket')
    parser.add_argument('--from-todo', dest='from_todo', help='Scribe note ID to seed from')
    parser.add_argument('--title', required=False, help='Short title')
    parser.add_argument('--problem', required=False, help='Problem statement')
    parser.add_argument('--description', required=False, help='Detailed description')
    parser.add_argument('--scope', required=False, help='Scope of work')
    parser.add_argument('--context', default='', help='Additional context (optional)')
    args = parser.parse_args()

    todo_content = None
    source = None
    if args.from_todo:
        todo_content = read_todo(args.from_todo)
        source = str(args.from_todo)

    description = args.description
    if description is None and todo_content is not None:
        description = todo_content

    missing = []
    if not args.title:
        missing.append('--title')
    if not args.problem:
        missing.append('--problem')
    if not description:
        missing.append('--description')
    if not args.scope:
        missing.append('--scope')
    if missing:
        parser.error(f'the following arguments are required: {", ".join(missing)}')

    slug = slugify(args.title)
    if not slug:
        print('Error: title produces an empty slug (remove special characters)', file=sys.stderr)
        sys.exit(1)
    BACKLOG_DIR.mkdir(parents=True, exist_ok=True)
    file_path = BACKLOG_DIR / f'{slug}.json'

    if file_path.exists():
        print(f'Error: backlog ticket {slug}.json already exists', file=sys.stderr)
        sys.exit(1)

    ticket = {
        'title': args.title,
        'problem': args.problem,
        'description': description,
        'scope': args.scope,
        'context': args.context,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    if source is not None:
        ticket['source'] = source

    file_path.write_text(json.dumps(ticket, indent=2), encoding='utf-8')
    print(str(file_path))


if __name__ == '__main__':
    main()
