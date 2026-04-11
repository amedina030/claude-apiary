#!/usr/bin/env python3
"""One-way migration: JSONL flat files -> folder-per-type layout (scribe v2).

Reads the legacy JSONL state files (notes.jsonl, notes_archive.jsonl,
learnings.jsonl) from the scribe state directory and writes them into the
folder-per-type layout managed by ScribeStore.

The migration is non-destructive by default: legacy files are backed up
before any writes. Pass --force to clear existing new-layout data first.
Pass --dry-run to see what would happen without touching the filesystem.

Usage:
    python scripts/migrate_scribe_v2.py [--dry-run] [--force] [--state-dir DIR]

By default:
  - state-dir = <git-repo-root(cwd)>/.apiary/scribe/
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scribe.store import (  # noqa: E402
    ScribeStore,
    TYPE_FOLDERS,
    LEARNING_FOLDER,
    ARCHIVE_DIRNAME,
    INDEX_FILENAME,
    NEXT_ID_FILENAME,
)
from scribe.notes import _git_repo_root  # noqa: E402

LEGACY_FILES = ('notes.jsonl', 'notes_archive.jsonl', 'learnings.jsonl')
VALID_TYPES = set(TYPE_FOLDERS.keys())
DEFAULT_TYPE = 'general'
SUMMARY_LIMIT = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_state_dir() -> 'Path | None':
    """Return <repo_root>/.apiary/scribe via _git_repo_root(), or None."""
    root = _git_repo_root()
    if root is None:
        return None
    return root / '.apiary' / 'scribe'


def _read_jsonl(path: Path) -> 'tuple[list[dict], int]':
    """Read a JSONL file. Returns (records, skipped_count).

    Missing file returns ([], 0). Malformed lines are counted and a warning
    is printed to stderr.
    """
    if not path.exists():
        return ([], 0)
    records: list[dict] = []
    skipped = 0
    for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            print(
                f'warning: skipping malformed line in {path}:{lineno}',
                file=sys.stderr,
            )
            skipped += 1
    return (records, skipped)


def _summary_from_content(content: str) -> str:
    """Generate a short summary from note content."""
    return content[:SUMMARY_LIMIT].replace('\n', ' ').strip()


def _new_layout_has_data(state_dir: Path) -> bool:
    """Return True if the new folder-per-type layout already contains data."""
    for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
        # Active index
        idx = state_dir / folder_name / INDEX_FILENAME
        if idx.exists() and idx.read_text(encoding='utf-8').strip():
            return True
        # Archive index
        arch_idx = state_dir / folder_name / ARCHIVE_DIRNAME / INDEX_FILENAME
        if arch_idx.exists() and arch_idx.read_text(encoding='utf-8').strip():
            return True
    return False


def _backup_legacy_files(state_dir: Path, dry_run: bool) -> dict:
    """Back up legacy JSONL files to state_dir/backup/<timestamp>_<name>.

    Returns a dict mapping each filename to a status string.
    """
    backup_dir = state_dir / 'backup'
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    result: dict[str, str] = {}
    for name in LEGACY_FILES:
        src = state_dir / name
        if not src.exists():
            result[name] = 'missing'
            continue
        dst = backup_dir / f'{ts}_{name}'
        if dry_run:
            result[name] = f'would-backup -> {dst}'
        else:
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result[name] = f'backed-up -> {dst}'
    return result


# ---------------------------------------------------------------------------
# Per-record converters
# ---------------------------------------------------------------------------

def _convert_note_record(store: ScribeStore, record: dict, archived: bool) -> None:
    """Write a single legacy note record into the new layout."""
    note_id = record.get('id')
    content: str = record.get('content', '') or ''
    note_type: str = record.get('type') or DEFAULT_TYPE
    if note_type not in VALID_TYPES:
        note_type = DEFAULT_TYPE

    session_id: str = record.get('session') or record.get('session_id') or ''
    timestamp: str = record.get('timestamp') or datetime.now(timezone.utc).isoformat()
    summary: str = record.get('summary') or _summary_from_content(content)

    entry: dict = {
        'id': int(note_id) if note_id is not None else 0,
        'type': note_type,
        'status': 'archived' if archived else 'active',
        'session': session_id,
        'timestamp': timestamp,
        'summary': summary,
        'has_body': True,
    }

    # Pass through any extra metadata keys not already handled (excluding 'content')
    skip_keys = {'id', 'type', 'status', 'session', 'session_id', 'timestamp', 'summary', 'has_body', 'content'}
    for k, v in record.items():
        if k not in skip_keys:
            entry.setdefault(k, v)

    # Determine target folder
    type_dir = store.state_dir / TYPE_FOLDERS[note_type]
    if archived:
        type_dir = type_dir / ARCHIVE_DIRNAME
    type_dir.mkdir(parents=True, exist_ok=True)

    store._append_index(type_dir, entry)
    (type_dir / f'{entry["id"]}.md').write_text(content, encoding='utf-8')


def _convert_learning_record(store: ScribeStore, record: dict) -> None:
    """Write a single legacy learning record into the new layout."""
    raw_id = record.get('id')
    if isinstance(raw_id, str) and raw_id[:1].lower() == 'l':
        numeric = int(raw_id[1:])
        l_prefixed = True
    else:
        numeric = int(raw_id) if raw_id is not None else 0
        l_prefixed = False

    content: str = record.get('content', '') or ''
    session_id: str = record.get('session') or record.get('session_id') or ''
    timestamp: str = record.get('timestamp') or datetime.now(timezone.utc).isoformat()
    summary: str = record.get('summary') or _summary_from_content(content)

    entry: dict = {
        'id': numeric,
        'type': 'learning',
        'status': 'active',
        'session': session_id,
        'timestamp': timestamp,
        'summary': summary,
        'has_body': True,
    }

    learn_dir = store.state_dir / LEARNING_FOLDER
    store._append_index(learn_dir, entry)

    filename = f'L{numeric}.md' if l_prefixed else f'{numeric}.md'
    (learn_dir / filename).write_text(content, encoding='utf-8')


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

def migrate(state_dir: Path, *, dry_run: bool = False, force: bool = False) -> dict:
    """Run the JSONL -> folder-per-type migration. Returns a structured report.

    The function is silent; the caller is responsible for printing the report.
    """
    report: dict = {
        'state_dir': str(state_dir),
        'dry_run': dry_run,
        'force': force,
        'legacy_found': False,
        'backup': {},
        'notes_migrated': 0,
        'archived_migrated': 0,
        'learnings_migrated': 0,
        'skipped': 0,
        'error': None,
    }

    legacy_paths = [state_dir / name for name in LEGACY_FILES]
    if not any(p.exists() for p in legacy_paths):
        report['error'] = f'No legacy data found at {state_dir}'
        return report

    report['legacy_found'] = True

    # Read all three source files upfront (safe for dry-run too)
    notes_records, skip1 = _read_jsonl(state_dir / 'notes.jsonl')
    archive_records, skip2 = _read_jsonl(state_dir / 'notes_archive.jsonl')
    learning_records, skip3 = _read_jsonl(state_dir / 'learnings.jsonl')
    report['skipped'] = skip1 + skip2 + skip3

    if dry_run:
        report['notes_migrated'] = len(notes_records)
        report['archived_migrated'] = len(archive_records)
        report['learnings_migrated'] = len(learning_records)
        report['backup'] = _backup_legacy_files(state_dir, dry_run=True)
        return report

    # Check whether the new layout already has data
    has_data = _new_layout_has_data(state_dir)
    if has_data and not force:
        report['error'] = 'New layout already contains data. Re-run with --force to overwrite.'
        return report

    if force and has_data:
        for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
            shutil.rmtree(state_dir / folder_name, ignore_errors=True)

    # Back up legacy files before writing anything
    report['backup'] = _backup_legacy_files(state_dir, dry_run=False)

    # Instantiate store — this also calls ensure_layout()
    store = ScribeStore(state_dir)

    # Dedup: if an id appears in both notes and archive, archive wins
    archive_ids = {r.get('id') for r in archive_records}
    # Within active list, keep the first occurrence of each id
    seen_active: set = set()
    active_records: list[dict] = []
    for r in notes_records:
        rid = r.get('id')
        if rid in archive_ids:
            continue
        if rid in seen_active:
            continue
        seen_active.add(rid)
        active_records.append(r)

    for record in archive_records:
        _convert_note_record(store, record, archived=True)
        report['archived_migrated'] += 1

    for record in active_records:
        _convert_note_record(store, record, archived=False)
        report['notes_migrated'] += 1

    for record in learning_records:
        _convert_learning_record(store, record)
        report['learnings_migrated'] += 1

    store._rebuild_next_id()
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_report(report: dict) -> None:
    dry = ' (DRY RUN)' if report['dry_run'] else ''
    print(f'Scribe v2 migration{dry}')
    print(f"  state_dir: {report['state_dir']}")

    if report['error']:
        print(f"  {report['error']}")
        return

    if report['backup']:
        print('  backup:')
        for name, status in report['backup'].items():
            print(f'    {name:<24} {status}')

    print(f"  notes migrated:      {report['notes_migrated']}")
    print(f"  archived migrated:   {report['archived_migrated']}")
    print(f"  learnings migrated:  {report['learnings_migrated']}")
    print(f"  skipped (malformed): {report['skipped']}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Migrate scribe JSONL state to folder-per-type layout.'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what would be migrated without writing anything')
    parser.add_argument('--force', action='store_true',
                        help='Clear existing new-layout data before migrating')
    parser.add_argument('--state-dir', type=Path, default=None,
                        help='Scribe state dir (default: <repo>/.apiary/scribe)')
    args = parser.parse_args()

    state_dir = args.state_dir or _default_state_dir()
    if state_dir is None:
        print(
            'error: --state-dir not specified and cwd not inside a git repo',
            file=sys.stderr,
        )
        sys.exit(2)

    state_dir = state_dir.expanduser()
    report = migrate(state_dir, dry_run=args.dry_run, force=args.force)
    _print_report(report)

    if report['error'] and 'No legacy data found' in report['error']:
        sys.exit(0)
    if report['error']:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
