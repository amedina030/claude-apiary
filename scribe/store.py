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
