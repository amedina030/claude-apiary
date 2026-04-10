"""Folder-per-type storage engine for scribe v2.

Provides ScribeStore — a class that manages notes and learnings using
individual .md files organized into type folders, each with its own
index.jsonl for fast listing.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo-root import for core.utils
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.utils.filelock import FileLock

# --- Constants ---

TYPE_FOLDERS: dict[str, str] = {
    'todo': 'todos',
    'handoff': 'handoffs',
    'decision': 'decisions',
    'wishlist': 'wishlists',
    'blocker': 'blockers',
    'context': 'context',
    'general': 'general',
}

LEARNING_FOLDER = 'learnings'

# All managed folder names (type folders + learnings)
_ALL_FOLDERS: list[str] = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]

INDEX_FILENAME = 'index.jsonl'
NEXT_ID_FILENAME = 'next_id'
ARCHIVE_DIRNAME = 'archive'


class ScribeStore:
    """Folder-per-type storage engine for notes and learnings.

    Initialized with a state_dir (Path). Manages folder layout,
    per-folder index.jsonl files, individual .md note files, and
    a global auto-increment counter.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.ensure_layout()

    def ensure_layout(self) -> None:
        """Create all type folders, archive subfolders, and next_id if missing."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for folder_name in _ALL_FOLDERS:
            folder = self.state_dir / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            # Create empty index.jsonl if it doesn't exist
            idx = folder / INDEX_FILENAME
            if not idx.exists():
                idx.write_text('', encoding='utf-8')
            # Create archive subfolder with its own empty index
            archive = folder / ARCHIVE_DIRNAME
            archive.mkdir(parents=True, exist_ok=True)
            archive_idx = archive / INDEX_FILENAME
            if not archive_idx.exists():
                archive_idx.write_text('', encoding='utf-8')
        # Create next_id file if missing
        nid_path = self.state_dir / NEXT_ID_FILENAME
        if not nid_path.exists():
            nid_path.write_text('1', encoding='utf-8')

    # --- Index I/O helpers ---

    @staticmethod
    def _read_index(type_dir: Path) -> list[dict]:
        """Read all valid entries from a folder's index.jsonl.

        Malformed lines are skipped with a warning to stderr.
        """
        idx_path = type_dir / INDEX_FILENAME
        if not idx_path.exists():
            return []
        entries = []
        for lineno, line in enumerate(idx_path.read_text(encoding='utf-8').splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"Warning: skipping malformed index entry at {idx_path}:{lineno}", file=sys.stderr)
        return entries

    @staticmethod
    def _write_index(type_dir: Path, entries: list[dict]) -> None:
        """Overwrite a folder's index.jsonl with the given entries.

        Uses FileLock on the index file for concurrent safety.
        """
        idx_path = type_dir / INDEX_FILENAME
        with FileLock(idx_path):
            lines = [json.dumps(e, separators=(',', ':')) for e in entries]
            idx_path.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')

    @staticmethod
    def _append_index(type_dir: Path, entry: dict) -> None:
        """Append a single entry to a folder's index.jsonl.

        Uses FileLock on the index file for concurrent safety.
        """
        idx_path = type_dir / INDEX_FILENAME
        with FileLock(idx_path):
            with open(idx_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, separators=(',', ':')) + '\n')

    # --- Global counter ---

    def _rebuild_next_id(self) -> int:
        """Scan all indexes for max ID and recreate the counter file.

        Returns the next ID to use (max found + 1, or 1 if no entries).
        """
        max_id = 0
        for folder_name in _ALL_FOLDERS:
            folder = self.state_dir / folder_name
            for entry in self._read_index(folder):
                eid = entry.get('id', 0)
                if isinstance(eid, int) and eid > max_id:
                    max_id = eid
            # Also scan archive
            archive = folder / ARCHIVE_DIRNAME
            for entry in self._read_index(archive):
                eid = entry.get('id', 0)
                if isinstance(eid, int) and eid > max_id:
                    max_id = eid
        next_id = max_id + 1
        nid_path = self.state_dir / NEXT_ID_FILENAME
        nid_path.write_text(str(next_id), encoding='utf-8')
        return next_id

    def _read_next_id(self) -> int:
        """Return current next_id value. Rebuilds if file is missing."""
        nid_path = self.state_dir / NEXT_ID_FILENAME
        if not nid_path.exists():
            return self._rebuild_next_id()
        text = nid_path.read_text(encoding='utf-8').strip()
        try:
            return int(text)
        except ValueError:
            return self._rebuild_next_id()

    def _increment_id(self) -> int:
        """Atomically read, increment, and write the next_id counter.

        Returns the ID that was consumed (i.e. the value before incrementing).
        Uses FileLock on the next_id file for concurrent safety.
        """
        nid_path = self.state_dir / NEXT_ID_FILENAME
        with FileLock(nid_path):
            current = self._read_next_id()
            nid_path.write_text(str(current + 1), encoding='utf-8')
        return current

    # --- Note file helpers ---

    def _type_dir(self, note_type: str) -> Path:
        """Return the folder Path for a given note type."""
        folder_name = TYPE_FOLDERS.get(note_type)
        if folder_name is None:
            raise ValueError(f"Unknown note type: {note_type!r}. Valid types: {list(TYPE_FOLDERS.keys())}")
        return self.state_dir / folder_name

    def _learning_dir(self) -> Path:
        """Return the folder Path for learnings."""
        return self.state_dir / LEARNING_FOLDER

    @staticmethod
    def _write_note_file(type_dir: Path, note_id: int, content: str) -> None:
        """Write note content to type_dir/<id>.md. Pure content, no frontmatter."""
        md_path = type_dir / f"{note_id}.md"
        md_path.write_text(content, encoding='utf-8')

    @staticmethod
    def _read_note_file(type_dir: Path, note_id: int) -> str | None:
        """Read note content from type_dir/<id>.md. Returns None if missing."""
        md_path = type_dir / f"{note_id}.md"
        if not md_path.exists():
            return None
        return md_path.read_text(encoding='utf-8')

    # --- CRUD operations: Notes ---

    def add_note(self, note_type: str, content: str, session_id: str,
                 summary: str = '', **metadata) -> dict:
        """Add a new note. Returns the created index entry dict."""
        type_dir = self._type_dir(note_type)
        note_id = self._increment_id()
        timestamp = datetime.now(timezone.utc).isoformat()
        # If no summary provided, use first 120 chars of content
        if not summary:
            summary = content[:120].replace('\n', ' ').strip()
        entry = {
            'id': note_id,
            'type': note_type,
            'status': 'active',
            'session': session_id,
            'timestamp': timestamp,
            'summary': summary,
            'has_body': bool(content),
            **metadata,
        }
        self._append_index(type_dir, entry)
        self._write_note_file(type_dir, note_id, content)
        return entry

    def get_note(self, note_id: int) -> dict | None:
        """Retrieve a single note by ID. Searches active then archive indexes.

        Returns a dict with index metadata plus 'content' key, or None.
        """
        # Search active indexes across all type folders
        for folder_name in TYPE_FOLDERS.values():
            type_dir = self.state_dir / folder_name
            for entry in self._read_index(type_dir):
                if entry.get('id') == note_id:
                    content = self._read_note_file(type_dir, note_id)
                    result = {**entry, 'content': content}
                    if content is None and entry.get('has_body'):
                        result['_warning'] = 'body_file_missing'
                    return result
            # Check archive
            archive_dir = type_dir / ARCHIVE_DIRNAME
            for entry in self._read_index(archive_dir):
                if entry.get('id') == note_id:
                    content = self._read_note_file(archive_dir, note_id)
                    result = {**entry, 'content': content, 'status': 'archived'}
                    if content is None and entry.get('has_body'):
                        result['_warning'] = 'body_file_missing'
                    return result
        return None

    def list_notes(self, note_type: str | None = None,
                   status: str = 'active',
                   search: str | None = None) -> list[dict]:
        """List notes, optionally filtered by type, status, and search term.

        Returns list of index entry dicts sorted by timestamp descending.
        """
        results: list[dict] = []
        # Determine which folders to scan
        if note_type is not None:
            folder_names = [TYPE_FOLDERS[note_type]]  # raises KeyError if invalid
        else:
            folder_names = list(TYPE_FOLDERS.values())

        for folder_name in folder_names:
            type_dir = self.state_dir / folder_name
            if status == 'active' or status == 'all':
                results.extend(self._read_index(type_dir))
            if status == 'archived' or status == 'all':
                archive_dir = type_dir / ARCHIVE_DIRNAME
                for entry in self._read_index(archive_dir):
                    entry_copy = {**entry, 'status': 'archived'}
                    results.append(entry_copy)

        # Filter by search term (case-insensitive in summary)
        if search:
            search_lower = search.lower()
            results = [e for e in results if search_lower in e.get('summary', '').lower()]

        # Sort by timestamp descending
        results.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
        return results

    def update_note(self, note_id: int, **kwargs) -> dict | None:
        """Update fields on an existing note's index entry.

        Supports updating: summary, status, content (rewrites .md file).
        Returns the updated entry dict, or None if not found.
        """
        content_update = kwargs.pop('content', None)
        for folder_name in TYPE_FOLDERS.values():
            type_dir = self.state_dir / folder_name
            entries = self._read_index(type_dir)
            for i, entry in enumerate(entries):
                if entry.get('id') == note_id:
                    entries[i] = {**entry, **kwargs}
                    self._write_index(type_dir, entries)
                    if content_update is not None:
                        self._write_note_file(type_dir, note_id, content_update)
                        entries[i]['has_body'] = bool(content_update)
                        self._write_index(type_dir, entries)
                    return entries[i]
        return None

    def archive_note(self, note_id: int) -> dict | None:
        """Move a note to its type folder's archive.

        Moves the index entry from type_dir/index.jsonl to
        type_dir/archive/index.jsonl, and moves <id>.md to archive/.
        Returns the archived entry dict, or None if not found.
        """
        for folder_name in TYPE_FOLDERS.values():
            type_dir = self.state_dir / folder_name
            entries = self._read_index(type_dir)
            target_entry = None
            remaining = []
            for entry in entries:
                if entry.get('id') == note_id:
                    target_entry = entry
                else:
                    remaining.append(entry)
            if target_entry is not None:
                # Remove from active index
                self._write_index(type_dir, remaining)
                # Add to archive index
                archive_dir = type_dir / ARCHIVE_DIRNAME
                archive_dir.mkdir(parents=True, exist_ok=True)
                target_entry['status'] = 'archived'
                self._append_index(archive_dir, target_entry)
                # Move .md file
                src_md = type_dir / f"{note_id}.md"
                dst_md = archive_dir / f"{note_id}.md"
                if src_md.exists():
                    dst_md.write_text(src_md.read_text(encoding='utf-8'), encoding='utf-8')
                    src_md.unlink()
                return target_entry
        return None

    # --- CRUD operations: Learnings ---

    def add_learning(self, content: str, session_id: str,
                     summary: str = '', **metadata) -> dict:
        """Add a new learning. Returns the created index entry dict."""
        learn_dir = self._learning_dir()
        note_id = self._increment_id()
        timestamp = datetime.now(timezone.utc).isoformat()
        if not summary:
            summary = content[:120].replace('\n', ' ').strip()
        entry = {
            'id': note_id,
            'type': 'learning',
            'status': 'active',
            'session': session_id,
            'timestamp': timestamp,
            'summary': summary,
            'has_body': bool(content),
            **metadata,
        }
        self._append_index(learn_dir, entry)
        self._write_note_file(learn_dir, note_id, content)
        return entry

    def list_learnings(self, search: str | None = None) -> list[dict]:
        """List all learnings, optionally filtered by search term."""
        learn_dir = self._learning_dir()
        entries = self._read_index(learn_dir)
        if search:
            search_lower = search.lower()
            entries = [e for e in entries if search_lower in e.get('summary', '').lower()]
        entries.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
        return entries

    def get_learning(self, learning_id: int) -> dict | None:
        """Retrieve a single learning by ID."""
        learn_dir = self._learning_dir()
        for entry in self._read_index(learn_dir):
            if entry.get('id') == learning_id:
                content = self._read_note_file(learn_dir, learning_id)
                result = {**entry, 'content': content}
                if content is None and entry.get('has_body'):
                    result['_warning'] = 'body_file_missing'
                return result
        return None

    def remove_learning(self, learning_id: int) -> dict | None:
        """Remove a learning by ID. Returns the removed entry or None."""
        learn_dir = self._learning_dir()
        entries = self._read_index(learn_dir)
        target = None
        remaining = []
        for entry in entries:
            if entry.get('id') == learning_id:
                target = entry
            else:
                remaining.append(entry)
        if target is not None:
            self._write_index(learn_dir, remaining)
            md_path = learn_dir / f"{learning_id}.md"
            if md_path.exists():
                md_path.unlink()
            return target
        return None
