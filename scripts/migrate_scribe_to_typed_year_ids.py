#!/usr/bin/env python3
"""One-way migration: folder-per-type integer-ID layout -> per-(type, year) sequential IDs.

Reads the v2 folder-per-type scribe state (integer-ID index entries, <id>.md body files)
and re-buckets into year subfolders with sequential seq numbers and display_id strings.

Usage:
    python scripts/migrate_scribe_to_typed_year_ids.py [--dry-run] [--force] [--state-dir DIR]
"""
import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scribe.store import (
    TYPE_FOLDERS,
    TYPE_PREFIXES,
    LEARNING_FOLDER,
    ARCHIVE_DIRNAME,
    INDEX_FILENAME,
    NEXT_SEQ_FILENAME,
)
from scribe.notes import _git_repo_root
from core.utils.filelock import FileLock

# All folders to process: type folders + learnings
_ALL_TYPE_FOLDERS: dict[str, str] = {**TYPE_FOLDERS}  # type_key -> folder_name
_LEARNING_TYPE = 'learning'

# Reference scan targets (relative to repo root)
_REF_SCAN_GLOBS: list[str] = [
    # memory files
    '.apiary/scribe/memory/*.md',
    # core commands
    'core/commands/*.md',
    # scribe CLAUDE.md
    'scribe/CLAUDE.md',
    # harden commands
    'harden/commands/*.md',
]


def _default_state_dir() -> Path | None:
    root = _git_repo_root()
    if root is None:
        return None
    return root / '.apiary' / 'scribe'


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL file, return list of parsed dicts. Skip malformed lines."""
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write records as JSONL. Uses FileLock for safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path):
        lines = [json.dumps(r, separators=(',', ':')) for r in records]
        path.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')


def _parse_year(timestamp_str: str | None) -> int:
    """Extract year from ISO 8601 timestamp string. Returns current UTC year on failure."""
    if not timestamp_str:
        return datetime.now(timezone.utc).year
    try:
        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        return dt.year
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).year


def _backup_state(state_dir: Path) -> Path:
    """Back up entire scribe state directory into state_dir/backup/<timestamp>_typed_year/.
    Returns the backup directory path."""
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_dir = state_dir / 'backup' / f'{ts}_typed_year'
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Copy all type folders, learnings folder, and migration_id_map.json if present
    for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
        src = state_dir / folder_name
        if src.exists():
            shutil.copytree(src, backup_dir / folder_name, dirs_exist_ok=True)
    # Copy existing migration_id_map.json if present
    map_file = state_dir / 'migration_id_map.json'
    if map_file.exists():
        shutil.copy2(map_file, backup_dir / 'migration_id_map.json')
    # Copy memory dir if present
    mem_dir = state_dir / 'memory'
    if mem_dir.exists():
        shutil.copytree(mem_dir, backup_dir / 'memory', dirs_exist_ok=True)
    return backup_dir


def _year_subfolder_has_data(state_dir: Path) -> bool:
    """Return True if any type folder has year subfolders with non-empty index.jsonl."""
    for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
        type_dir = state_dir / folder_name
        if not type_dir.exists():
            continue
        for child in type_dir.iterdir():
            if not child.is_dir() or not child.name.isdigit():
                continue
            idx = child / INDEX_FILENAME
            if idx.exists() and idx.read_text(encoding='utf-8').strip():
                return True
            arch_idx = child / ARCHIVE_DIRNAME / INDEX_FILENAME
            if arch_idx.exists() and arch_idx.read_text(encoding='utf-8').strip():
                return True
    return False


def _has_v2_data(state_dir: Path) -> bool:
    """Return True if any type folder has a top-level index.jsonl with entries (v2 integer-ID format)."""
    for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
        idx = state_dir / folder_name / INDEX_FILENAME
        if idx.exists() and idx.read_text(encoding='utf-8').strip():
            return True
        arch_idx = state_dir / folder_name / ARCHIVE_DIRNAME / INDEX_FILENAME
        if arch_idx.exists() and arch_idx.read_text(encoding='utf-8').strip():
            return True
    return False


def _collect_entries(state_dir: Path) -> dict:
    """Collect all entries from v2 layout. Returns dict keyed by (type_key, 'active'|'archived') -> list[dict].

    For note type folders: reads top-level index.jsonl (active) and archive/index.jsonl (archived).
    For learnings: reads learnings/index.jsonl (active) only.
    Each entry dict gets '_body_content' key added (str, may be empty if file missing).
    Also returns counts: missing_body_count, timestamp_fallback_count.
    """
    result: dict[tuple[str, str], list[dict]] = {}
    missing_body_count = 0
    timestamp_fallback_count = 0

    # Process note type folders
    for type_key, folder_name in TYPE_FOLDERS.items():
        type_dir = state_dir / folder_name
        # Active entries from top-level index.jsonl
        active_entries = _read_jsonl(type_dir / INDEX_FILENAME)
        for entry in active_entries:
            entry['_type_key'] = type_key
            # Read body file: <folder>/<id>.md
            eid = entry.get('id', 0)
            body_path = type_dir / f'{eid}.md'
            if body_path.exists():
                entry['_body_content'] = body_path.read_text(encoding='utf-8')
            else:
                entry['_body_content'] = ''
                if entry.get('has_body', False):
                    missing_body_count += 1
            # Check timestamp
            ts = entry.get('timestamp')
            year = _parse_year(ts)
            if not ts or year == datetime.now(timezone.utc).year and not ts:
                timestamp_fallback_count += 1
            entry['_year'] = year
        result[(type_key, 'active')] = active_entries

        # Archived entries from archive/index.jsonl
        archive_dir = type_dir / ARCHIVE_DIRNAME
        archived_entries = _read_jsonl(archive_dir / INDEX_FILENAME)
        for entry in archived_entries:
            entry['_type_key'] = type_key
            eid = entry.get('id', 0)
            body_path = archive_dir / f'{eid}.md'
            if body_path.exists():
                entry['_body_content'] = body_path.read_text(encoding='utf-8')
            else:
                entry['_body_content'] = ''
                if entry.get('has_body', False):
                    missing_body_count += 1
            ts = entry.get('timestamp')
            year = _parse_year(ts)
            if not ts:
                timestamp_fallback_count += 1
            entry['_year'] = year
        result[(type_key, 'archived')] = archived_entries

    # Process learnings
    learn_dir = state_dir / LEARNING_FOLDER
    learn_entries = _read_jsonl(learn_dir / INDEX_FILENAME)
    for entry in learn_entries:
        entry['_type_key'] = _LEARNING_TYPE
        raw_id = entry.get('id')
        # Learnings may have L-prefixed IDs like 'L3' or bare ints
        if isinstance(raw_id, str) and raw_id[:1].upper() == 'L':
            numeric_id = raw_id[1:]
            body_path = learn_dir / f'L{numeric_id}.md'
            if not body_path.exists():
                body_path = learn_dir / f'{numeric_id}.md'
        else:
            numeric_id = str(raw_id) if raw_id is not None else '0'
            body_path = learn_dir / f'{numeric_id}.md'
            if not body_path.exists():
                body_path = learn_dir / f'L{numeric_id}.md'
        if body_path.exists():
            entry['_body_content'] = body_path.read_text(encoding='utf-8')
        else:
            entry['_body_content'] = ''
            if entry.get('has_body', False):
                missing_body_count += 1
        ts = entry.get('timestamp')
        year = _parse_year(ts)
        if not ts:
            timestamp_fallback_count += 1
        entry['_year'] = year
    result[(_LEARNING_TYPE, 'active')] = learn_entries

    return {
        'entries': result,
        'missing_body_count': missing_body_count,
        'timestamp_fallback_count': timestamp_fallback_count,
    }


def _dedup_entries(entries_by_key: dict[tuple[str, str], list[dict]]) -> tuple[dict, int]:
    """Dedup: for each type, if same id appears in both active and archive, archive wins.
    Returns (deduped entries_by_key, dropped_count)."""
    dropped = 0
    # Build set of archived IDs per type_key
    archived_ids: dict[str, set] = {}
    for (type_key, status), entries in entries_by_key.items():
        if status == 'archived':
            ids = set()
            for e in entries:
                ids.add(e.get('id'))
            archived_ids[type_key] = ids

    # Filter active entries
    for (type_key, status), entries in list(entries_by_key.items()):
        if status == 'active':
            arch_ids = archived_ids.get(type_key, set())
            filtered = []
            for e in entries:
                if e.get('id') in arch_ids:
                    dropped += 1
                else:
                    filtered.append(e)
            entries_by_key[(type_key, status)] = filtered

    return entries_by_key, dropped


def _bucket_and_assign_seqs(entries_by_key: dict[tuple[str, str], list[dict]]) -> dict:
    """Group entries by (type_key, year, status), sort by timestamp, assign sequential seq numbers.

    Returns dict: (type_key, year, status) -> list[dict] where each dict has:
      'display_id', 'type', 'year', 'seq', 'status', 'session', 'timestamp',
      'summary', 'has_body', '_body_content', '_old_id' plus any extra metadata.

    Seq counters are shared between active and archived within the same (type_key, year),
    with archived entries getting their seq first (they are older).
    """
    # Collect all entries grouped by (type_key, year)
    buckets: dict[tuple[str, int], list[tuple[dict, str]]] = {}  # key -> list of (entry, status)
    for (type_key, status), entries in entries_by_key.items():
        for entry in entries:
            year = entry['_year']
            key = (type_key, year)
            buckets.setdefault(key, []).append((entry, status))

    # Sort each bucket by timestamp (stable sort preserves original order for ties)
    for key, items in buckets.items():
        items.sort(key=lambda x: x[0].get('timestamp', ''))

    # Assign seq numbers and build output
    result: dict[tuple[str, int, str], list[dict]] = {}
    for (type_key, year), items in buckets.items():
        seq = 1
        for entry, status in items:
            prefix = TYPE_PREFIXES.get(type_key, 'G')
            display_id = f'{prefix}-{year}-{seq}'
            old_id = entry.get('id')

            new_entry = {
                'display_id': display_id,
                'type': type_key if type_key != _LEARNING_TYPE else 'learning',
                'year': year,
                'seq': seq,
                'status': 'archived' if status == 'archived' else 'active',
                'session': entry.get('session', '') or entry.get('session_id', ''),
                'timestamp': entry.get('timestamp', ''),
                'summary': entry.get('summary', ''),
                'has_body': entry.get('has_body', bool(entry.get('_body_content'))),
                '_body_content': entry.get('_body_content', ''),
                '_old_id': old_id,
                '_type_key': type_key,
            }
            # Carry through extra metadata keys
            skip_keys = {'id', 'type', 'status', 'session', 'session_id', 'timestamp',
                         'summary', 'has_body', 'content', '_body_content', '_type_key',
                         '_year', '_old_id', 'display_id', 'year', 'seq'}
            for k, v in entry.items():
                if k not in skip_keys:
                    new_entry.setdefault(k, v)

            out_key = (type_key, year, status)
            result.setdefault(out_key, []).append(new_entry)
            seq += 1

    return result


def _build_migration_map(bucketed: dict) -> dict[str, str]:
    """Build migration_id_map: old_id (str) -> new display_id.
    For learnings, map both 'L<n>' and '<n>' to the new display_id."""
    id_map: dict[str, str] = {}
    for (type_key, year, status), entries in bucketed.items():
        for entry in entries:
            old_id = entry.get('_old_id')
            display_id = entry['display_id']
            if old_id is None:
                continue
            if type_key == _LEARNING_TYPE:
                # Map both 'L<n>' and '<n>' forms
                if isinstance(old_id, str) and old_id[:1].upper() == 'L':
                    numeric = old_id[1:]
                    id_map[old_id] = display_id  # 'L3' -> 'L-2026-2'
                    id_map[numeric] = display_id  # '3' -> 'L-2026-2'
                else:
                    numeric = str(old_id)
                    id_map[numeric] = display_id
                    id_map[f'L{numeric}'] = display_id
            else:
                id_map[str(old_id)] = display_id
    return id_map


def _write_migrated_data(state_dir: Path, bucketed: dict) -> None:
    """Write new body files, index.jsonl files, and next_seq counters for all buckets."""
    # Track max seq per (type_key, year) for next_seq
    max_seq: dict[tuple[str, int], int] = {}

    for (type_key, year, status), entries in bucketed.items():
        # Determine folder
        if type_key == _LEARNING_TYPE:
            folder_name = LEARNING_FOLDER
        else:
            folder_name = TYPE_FOLDERS[type_key]

        if status == 'archived':
            target_dir = state_dir / folder_name / str(year) / ARCHIVE_DIRNAME
        else:
            target_dir = state_dir / folder_name / str(year)

        target_dir.mkdir(parents=True, exist_ok=True)

        # Write body files
        index_entries: list[dict] = []
        for entry in entries:
            seq = entry['seq']
            body = entry.pop('_body_content', '')
            old_id = entry.pop('_old_id', None)
            entry.pop('_type_key', None)

            # Write body .md file
            body_path = target_dir / f'{seq}.md'
            body_path.write_text(body, encoding='utf-8')

            index_entries.append(entry)

            # Track max seq
            key = (type_key, year)
            if key not in max_seq or seq > max_seq[key]:
                max_seq[key] = seq

        # Write index.jsonl for this (type, year[, archive]) directory
        _write_jsonl(target_dir / INDEX_FILENAME, index_entries)

    # Write next_seq files
    for (type_key, year), ms in max_seq.items():
        if type_key == _LEARNING_TYPE:
            folder_name = LEARNING_FOLDER
        else:
            folder_name = TYPE_FOLDERS[type_key]
        year_dir = state_dir / folder_name / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        seq_path = year_dir / NEXT_SEQ_FILENAME
        seq_path.write_text(str(ms + 1), encoding='utf-8')
        # Also ensure archive dir exists
        (year_dir / ARCHIVE_DIRNAME).mkdir(parents=True, exist_ok=True)
        arch_idx = year_dir / ARCHIVE_DIRNAME / INDEX_FILENAME
        if not arch_idx.exists():
            arch_idx.write_text('', encoding='utf-8')


def _rewrite_references(state_dir: Path, id_map: dict[str, str]) -> dict:
    """Scan target files for old-format references and rewrite them.

    Patterns to replace:
      - #<digits> -> new display_id (only if digits are in id_map)
      - #L<digits> -> new display_id
      - 'notes.py get <digits>' -> 'notes.py get <display_id>'

    Also scans new-layout handoff and learning body files.
    Returns {'files_rewritten': int, 'refs_rewritten': int, 'unresolved_refs': int}.
    """
    result = {'files_rewritten': 0, 'refs_rewritten': 0, 'unresolved_refs': 0}
    if not id_map:
        return result

    # Build list of files to scan
    repo_root = state_dir.parent.parent  # .apiary/scribe -> repo root
    files_to_scan: list[Path] = []

    # Scan targets from repo root
    for glob_pattern in _REF_SCAN_GLOBS:
        files_to_scan.extend(repo_root.glob(glob_pattern))

    # Also scan new-layout handoff and learning body .md files
    for folder_name in [TYPE_FOLDERS.get('handoff', 'handoffs'), LEARNING_FOLDER]:
        folder = state_dir / folder_name
        if folder.exists():
            for year_dir in folder.iterdir():
                if year_dir.is_dir() and year_dir.name.isdigit():
                    files_to_scan.extend(year_dir.glob('*.md'))
                    arch = year_dir / ARCHIVE_DIRNAME
                    if arch.exists():
                        files_to_scan.extend(arch.glob('*.md'))

    # Regex patterns
    # #L<digits> or #<digits> (not preceded by another word char to avoid false matches on hex colors etc.)
    ref_pattern = re.compile(r'(?<!\w)#(L?)(\d+)(?!\d)')
    # notes.py get <digits>
    get_pattern = re.compile(r'(notes\.py\s+get\s+)(\d+)')

    for fpath in files_to_scan:
        if not fpath.exists() or not fpath.is_file():
            continue
        try:
            content = fpath.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue

        new_content = content
        local_rewrites = 0
        local_unresolved = 0

        def replace_ref(m):
            nonlocal local_rewrites, local_unresolved
            prefix = m.group(1)  # '' or 'L'
            digits = m.group(2)
            lookup_key = f'{prefix}{digits}' if prefix else digits
            if lookup_key in id_map:
                local_rewrites += 1
                return id_map[lookup_key]
            else:
                local_unresolved += 1
                return m.group(0)

        new_content = ref_pattern.sub(replace_ref, new_content)

        def replace_get(m):
            nonlocal local_rewrites, local_unresolved
            cmd_prefix = m.group(1)
            digits = m.group(2)
            if digits in id_map:
                local_rewrites += 1
                return cmd_prefix + id_map[digits]
            else:
                local_unresolved += 1
                return m.group(0)

        new_content = get_pattern.sub(replace_get, new_content)

        if new_content != content:
            fpath.write_text(new_content, encoding='utf-8')
            result['files_rewritten'] += 1
        result['refs_rewritten'] += local_rewrites
        result['unresolved_refs'] += local_unresolved

    return result


def _cleanup_old_files(state_dir: Path) -> int:
    """Remove old top-level index.jsonl and integer-named .md body files from type folders.
    Also remove old next_id file. Returns count of files removed."""
    removed = 0
    for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
        type_dir = state_dir / folder_name
        if not type_dir.exists():
            continue
        # Remove top-level index.jsonl
        idx = type_dir / INDEX_FILENAME
        if idx.exists():
            idx.unlink()
            removed += 1
        # Remove integer-named or L-prefixed .md files at top level
        for md_file in type_dir.glob('*.md'):
            stem = md_file.stem
            if stem.isdigit() or (stem[:1].upper() == 'L' and stem[1:].isdigit()):
                md_file.unlink()
                removed += 1
        # Remove archive top-level index.jsonl and integer .md files
        arch_dir = type_dir / ARCHIVE_DIRNAME
        if arch_dir.exists() and arch_dir.is_dir():
            # Only remove if archive is a direct child (not inside year subfolder)
            arch_idx = arch_dir / INDEX_FILENAME
            if arch_idx.exists():
                arch_idx.unlink()
                removed += 1
            for md_file in arch_dir.glob('*.md'):
                stem = md_file.stem
                if stem.isdigit() or (stem[:1].upper() == 'L' and stem[1:].isdigit()):
                    md_file.unlink()
                    removed += 1
    # Remove old next_id file
    nid = state_dir / 'next_id'
    if nid.exists():
        nid.unlink()
        removed += 1
    return removed


def migrate(state_dir: Path, *, dry_run: bool = False, force: bool = False) -> dict:
    """Run the v2 integer-ID -> per-(type, year) migration. Returns a structured report."""
    report: dict = {
        'state_dir': str(state_dir),
        'dry_run': dry_run,
        'force': force,
        'error': None,
        'notes_migrated': 0,
        'learnings_migrated': 0,
        'archived_migrated': 0,
        'duplicates_dropped': 0,
        'missing_body_count': 0,
        'timestamp_fallback_count': 0,
        'refs_rewritten': 0,
        'unresolved_refs': 0,
        'files_rewritten': 0,
        'old_files_removed': 0,
        'backup_dir': None,
        'migration_map_path': None,
    }

    # Check for v2 data
    if not _has_v2_data(state_dir):
        report['error'] = f'No v2 data found at {state_dir}'
        return report

    # Check if year subfolders already have data
    if _year_subfolder_has_data(state_dir) and not force:
        report['error'] = 'Year-subfolder layout already contains data. Re-run with --force to overwrite.'
        return report

    # Collect all entries
    collected = _collect_entries(state_dir)
    entries_by_key = collected['entries']
    report['missing_body_count'] = collected['missing_body_count']
    report['timestamp_fallback_count'] = collected['timestamp_fallback_count']

    # Dedup
    entries_by_key, dropped = _dedup_entries(entries_by_key)
    report['duplicates_dropped'] = dropped

    # Bucket and assign seqs
    bucketed = _bucket_and_assign_seqs(entries_by_key)

    # Build migration map
    id_map = _build_migration_map(bucketed)

    # Count what will be migrated
    for (type_key, year, status), entries in bucketed.items():
        if type_key == _LEARNING_TYPE:
            report['learnings_migrated'] += len(entries)
        elif status == 'archived':
            report['archived_migrated'] += len(entries)
        else:
            report['notes_migrated'] += len(entries)

    if dry_run:
        report['migration_map_sample'] = dict(list(id_map.items())[:10])
        return report

    # Backup
    backup_dir = _backup_state(state_dir)
    report['backup_dir'] = str(backup_dir)

    # If force and year subfolders have data, clear them
    if force and _year_subfolder_has_data(state_dir):
        for folder_name in list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]:
            type_dir = state_dir / folder_name
            if not type_dir.exists():
                continue
            for child in list(type_dir.iterdir()):
                if child.is_dir() and child.name.isdigit():
                    shutil.rmtree(child)

    # Write migrated data
    _write_migrated_data(state_dir, bucketed)

    # Write migration_id_map.json
    map_path = state_dir / 'migration_id_map.json'
    map_path.write_text(json.dumps(id_map, indent=2), encoding='utf-8')
    report['migration_map_path'] = str(map_path)

    # Rewrite references
    ref_result = _rewrite_references(state_dir, id_map)
    report['refs_rewritten'] = ref_result['refs_rewritten']
    report['unresolved_refs'] = ref_result['unresolved_refs']
    report['files_rewritten'] = ref_result['files_rewritten']

    # Cleanup old files
    report['old_files_removed'] = _cleanup_old_files(state_dir)

    return report


def _print_report(report: dict) -> None:
    dry = ' (DRY RUN)' if report['dry_run'] else ''
    print(f'Scribe typed-year-ID migration{dry}')
    print(f"  state_dir: {report['state_dir']}")
    if report['error']:
        print(f"  ERROR: {report['error']}")
        return
    print(f"  notes migrated:        {report['notes_migrated']}")
    print(f"  archived migrated:     {report['archived_migrated']}")
    print(f"  learnings migrated:    {report['learnings_migrated']}")
    print(f"  duplicates dropped:    {report['duplicates_dropped']}")
    print(f"  missing bodies:        {report['missing_body_count']}")
    print(f"  timestamp fallbacks:   {report['timestamp_fallback_count']}")
    print(f"  refs rewritten:        {report['refs_rewritten']}")
    print(f"  unresolved refs:       {report['unresolved_refs']}")
    print(f"  files rewritten:       {report['files_rewritten']}")
    print(f"  old files removed:     {report['old_files_removed']}")
    if report.get('backup_dir'):
        print(f"  backup:                {report['backup_dir']}")
    if report.get('migration_map_path'):
        print(f"  migration map:         {report['migration_map_path']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Migrate scribe v2 integer-ID layout to per-(type, year) sequential IDs.'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what would be migrated without writing anything')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing year-subfolder data')
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

    if report['error']:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
