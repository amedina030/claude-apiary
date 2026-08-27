#!/usr/bin/env python3
"""Promote a backlog ticket to runner intake.

Validates, assigns a UUID, copies to intake/, and removes the backlog file.

Usage:
    promote.py <slug>
"""
import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runner.target_repo import backlog_dir, intake_dir

SCRIPT_DIR = Path(__file__).resolve().parent
BACKLOG_DIR = backlog_dir()
INTAKE_DIR = intake_dir()
REPO_ROOT = SCRIPT_DIR.parent


def main():
    parser = argparse.ArgumentParser(
        description="Promote a backlog ticket to runner intake.",
    )
    parser.add_argument(
        "slug",
        help="Backlog ticket slug — the filename without directory or .json extension",
    )
    slug = parser.parse_args().slug
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
    intake_id = data.get('id') or str(uuid.uuid4())

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
        [sys.executable, "-m", "runner.validate_intake", str(intake_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        intake_path.unlink(missing_ok=True)
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(1)

    backlog_path.unlink()
    print(str(intake_path))
    print(intake_id)


if __name__ == '__main__':
    main()
