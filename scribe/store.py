"""Folder-per-type storage engine for scribe v2.

Provides ScribeStore — a class that manages notes and learnings using
individual .md files organized into type folders, each with its own
index.jsonl for fast listing.
"""
import json
import os
import re
import sys
import tempfile
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
    'reference': 'references',
}

TYPE_PREFIXES: dict[str, str] = {
    'todo': 'T',
    'handoff': 'H',
    'decision': 'D',
    'wishlist': 'W',
    'reference': 'R',
    'blocker': 'B',
    'context': 'C',
    'general': 'G',
    'learning': 'L',
}

LEARNING_FOLDER = 'learnings'

# All managed folder names (type folders + learnings)
_ALL_FOLDERS: list[str] = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]

INDEX_FILENAME = 'index.jsonl'
NEXT_SEQ_FILENAME = 'next_seq'
ARCHIVE_DIRNAME = 'archive'

# Brief-summary cap — shorter than `summary` (300), aimed at one-line display
# in the GUI sidebar. Lives next to summary on each index entry.
BRIEF_SUMMARY_MAX = 120

# Fields serialized into learning .md frontmatter. Order is fixed to keep
# diffs stable across re-writes.
_LEARNING_FRONTMATTER_FIELDS = ('tags', 'areas', 'supersedes')


def _format_learning_content(content: str, frontmatter: dict | None = None) -> str:
    """Prefix ``content`` with a ``---`` frontmatter block when any of the
    supported fields are present. Returns ``content`` unchanged when the
    frontmatter dict is empty — so legacy learnings stay legacy-shaped.
    """
    if not frontmatter:
        return content
    rendered: list[str] = []
    for key in _LEARNING_FRONTMATTER_FIELDS:
        value = frontmatter.get(key)
        if value is None or value == [] or value == '':
            continue
        if isinstance(value, (list, tuple)):
            rendered.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            rendered.append(f"{key}: {value}")
    if not rendered:
        return content
    return '---\n' + '\n'.join(rendered) + '\n---\n' + content


def _parse_learning_content(text: str) -> tuple[dict, str]:
    """Split a learning .md into ``(frontmatter_dict, body)``.

    Tolerant of files without frontmatter — returns ``({}, text)`` in that
    case so legacy 102-learning corpus keeps working. Malformed frontmatter
    (missing closing fence, bad lines) silently falls back to the same
    empty-fm path rather than raising, because scribe callers on the hot
    PreToolUse path cannot afford to crash on a hand-edited .md.
    """
    if not text:
        return {}, text
    if not (text.startswith('---\n') or text.startswith('---\r\n')):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        return {}, text
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i] == '---':
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm: dict = {}
    for raw in lines[1:end_idx]:
        line = raw.rstrip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        value = value.strip()
        if value == '[]':
            fm[key] = []
        elif value.startswith('[') and value.endswith(']'):
            inner = value[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [item.strip().strip('"').strip("'") for item in inner.split(',') if item.strip()]
        else:
            fm[key] = value.strip('"').strip("'")
    body_lines = lines[end_idx + 1:]
    body = '\n'.join(body_lines)
    if text.endswith('\n') and body and not body.endswith('\n'):
        body += '\n'
    return fm, body


def derive_brief_summary(content: str) -> str:
    """Produce a short, readable brief_summary from note content.

    Preference order (all capped at BRIEF_SUMMARY_MAX):
    1. Markdown heading line — first line, stripped of leading #s.
    2. Sentence end (``.!?``) within the cap.
    3. Closing paren ``)`` at position >= 30 — keeps informative
       parentheticals like "(decided 2026-04-06)" or "(not just tool calls)"
       and stops cleanly right after them.
    4. Colon ``:`` followed by whitespace at position >= 10 — catches
       "header: details" notes where the header is a clean brief.
    5. Last word boundary within the cap, with an ellipsis suffix.
    """
    s = (content or '').strip()
    if not s:
        return ''
    first_nl = s.find('\n')
    if re.match(r'^#{1,6}\s', s):
        head_line = s if first_nl == -1 else s[:first_nl]
        head = head_line.lstrip('#').strip()
        return head[:BRIEF_SUMMARY_MAX].rstrip()
    flat = re.sub(r'\s+', ' ', s).strip()
    window = flat[:BRIEF_SUMMARY_MAX]
    sent = re.search(r'[.!?](?=\s|$)', window)
    if sent:
        return window[:sent.end()].rstrip()
    paren_close = window.find(')')
    if paren_close >= 30:
        return window[:paren_close + 1].rstrip()
    colon = re.search(r':(?=\s)', window)
    if colon and colon.start() >= 10:
        return window[:colon.end()].rstrip()
    # Em-dash (U+2014) or double-hyphen often separates a clause from its
    # elaboration ("X foo — does Y"); cut just before it.
    dash = re.search(r'\s[—–]\s|\s--\s', window)
    if dash and dash.start() >= 30:
        return window[:dash.start()].rstrip()
    if len(flat) <= BRIEF_SUMMARY_MAX:
        return flat
    # Last-resort deep comma cut — only if well into the brief so we don't
    # chop trivially short. Drops the trailing fragment cleanly.
    comma = window.rfind(',')
    if comma >= 60:
        return window[:comma].rstrip()
    last_space = window.rfind(' ')
    if last_space > 40:
        return window[:last_space].rstrip() + '…'
    return window.rstrip() + '…'


def derive_summary(content: str) -> str:
    """First non-empty, stripped line of *content*, truncated to 300 chars.

    The summary rule adopted from the source scribe (spec §5.6). Distinct from
    ``derive_brief_summary`` (the GUI sidebar's <=120-char heuristic), which is
    intentionally left unchanged.
    """
    for line in (content or '').splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:300]
    return ''


class ScribeStore:
    """Folder-per-type storage engine for notes and learnings.

    Initialized with a state_dir (Path). Manages folder layout,
    per-folder index.jsonl files, individual .md note files, and
    per-(type,year) sequence counters.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.ensure_layout()

    def ensure_layout(self) -> None:
        """Create all type folders, year subfolders, and counters if missing."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        year = datetime.now(timezone.utc).year
        for folder_name in _ALL_FOLDERS:
            folder = self.state_dir / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            # Create current-year subfolder
            year_dir = folder / str(year)
            year_dir.mkdir(parents=True, exist_ok=True)
            year_idx = year_dir / INDEX_FILENAME
            if not year_idx.exists():
                year_idx.write_text('', encoding='utf-8')
            seq_path = year_dir / NEXT_SEQ_FILENAME
            if not seq_path.exists():
                seq_path.write_text('1', encoding='utf-8')
            year_archive = year_dir / ARCHIVE_DIRNAME
            year_archive.mkdir(parents=True, exist_ok=True)
            year_archive_idx = year_archive / INDEX_FILENAME
            if not year_archive_idx.exists():
                year_archive_idx.write_text('', encoding='utf-8')

    # --- Index I/O helpers ---

    @staticmethod
    def _read_index(type_dir: Path, strict: bool = False) -> list[dict]:
        """Read all valid entries from a folder's index.jsonl.

        Malformed lines are skipped with a warning to stderr. If strict=True,
        raise RuntimeError on the first malformed line instead. Callers that
        rely on the index being complete (e.g. sequence counter rebuild)
        should pass strict=True so a corrupted index cannot silently cause
        undercounting and subsequent ID collisions.
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
            except json.JSONDecodeError as e:
                if strict:
                    raise RuntimeError(
                        f"Malformed index entry at {idx_path}:{lineno} ({e.msg}). "
                        f"Refusing to proceed — a rebuild over a corrupted index "
                        f"would undercount seq and cause ID collisions. "
                        f"Inspect and repair {idx_path} before retrying."
                    ) from e
                print(f"Warning: skipping malformed index entry at {idx_path}:{lineno}", file=sys.stderr)
        return entries

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write *text* to *path* atomically: temp file in the same dir, then
        ``os.replace``. A process kill mid-write leaves either the old file or
        the new one intact — never a torn line. Cleans up the temp file if the
        replace doesn't happen. Caller holds the FileLock on *path*.
        """
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _write_index(type_dir: Path, entries: list[dict]) -> None:
        """Overwrite a folder's index.jsonl with the given entries.

        Atomic (temp + os.replace) under a FileLock for concurrent safety.
        """
        idx_path = type_dir / INDEX_FILENAME
        with FileLock(idx_path):
            lines = [json.dumps(e, separators=(',', ':')) for e in entries]
            ScribeStore._atomic_write(idx_path, '\n'.join(lines) + ('\n' if lines else ''))

    @staticmethod
    def _append_index(type_dir: Path, entry: dict) -> None:
        """Append a single entry to a folder's index.jsonl.

        Read-modify-write under a FileLock, flushed atomically (temp +
        os.replace) so a crash mid-append cannot leave a partial line.
        """
        idx_path = type_dir / INDEX_FILENAME
        with FileLock(idx_path):
            existing = idx_path.read_text(encoding='utf-8') if idx_path.exists() else ''
            if existing and not existing.endswith('\n'):
                existing += '\n'
            ScribeStore._atomic_write(idx_path, existing + json.dumps(entry, separators=(',', ':')) + '\n')

    # --- Per-(type,year) sequence counter ---

    def _year_dir(self, type_dir: Path, year: int) -> Path:
        """Return type_dir/<year>. Does NOT create it."""
        return type_dir / str(year)

    def _ensure_year_dir(self, type_dir: Path, year: int) -> Path:
        """Ensure type_dir/<year> exists with index, next_seq, and archive. Returns it."""
        year_dir = type_dir / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        idx = year_dir / INDEX_FILENAME
        if not idx.exists():
            idx.write_text('', encoding='utf-8')
        seq_path = year_dir / NEXT_SEQ_FILENAME
        if not seq_path.exists():
            self._rebuild_next_seq(year_dir)
        archive = year_dir / ARCHIVE_DIRNAME
        archive.mkdir(parents=True, exist_ok=True)
        archive_idx = archive / INDEX_FILENAME
        if not archive_idx.exists():
            archive_idx.write_text('', encoding='utf-8')
        return year_dir

    def _rebuild_next_seq(self, year_dir: Path) -> int:
        """Scan year_dir's index.jsonl (and archive) for max 'seq'. Write and return next_seq.

        Uses strict index reads — a malformed line in either index raises
        RuntimeError rather than being skipped, so a corrupted index cannot
        silently undercount and produce colliding IDs on the next write.
        """
        max_seq = 0
        for entry in self._read_index(year_dir, strict=True):
            s = entry.get('seq', 0)
            if isinstance(s, int) and s > max_seq:
                max_seq = s
        archive_dir = year_dir / ARCHIVE_DIRNAME
        for entry in self._read_index(archive_dir, strict=True):
            s = entry.get('seq', 0)
            if isinstance(s, int) and s > max_seq:
                max_seq = s
        next_seq = max_seq + 1
        seq_path = year_dir / NEXT_SEQ_FILENAME
        seq_path.write_text(str(next_seq), encoding='utf-8')
        return next_seq

    def _increment_seq(self, year_dir: Path) -> int:
        """Atomically consume the next sequence number for a year_dir.

        Returns the sequence number that was consumed.
        Uses FileLock on the next_seq file for concurrent safety.
        """
        seq_path = year_dir / NEXT_SEQ_FILENAME
        with FileLock(seq_path):
            if not seq_path.exists():
                current = self._rebuild_next_seq(year_dir)
            else:
                text = seq_path.read_text(encoding='utf-8').strip()
                try:
                    current = int(text)
                except ValueError:
                    current = self._rebuild_next_seq(year_dir)
            seq_path.write_text(str(current + 1), encoding='utf-8')
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

    def _read_body_any(self, note_type: str, year: int, seq: int) -> str | None:
        """Read a note body from the active dir, falling back to archive. None if absent.

        Used only by the search path (lazy body match) — never on the hot
        index/startup path, which does not pass a search term.
        """
        try:
            type_dir = self._type_dir(note_type)
        except ValueError:
            return None
        year_dir = type_dir / str(year)
        body = self._read_note_file(year_dir, seq)
        if body is not None:
            return body
        return self._read_note_file(year_dir / ARCHIVE_DIRNAME, seq)

    def _read_learning_body_any(self, year: int, seq: int) -> str | None:
        """Read a learning body (frontmatter included) from active, then archive."""
        year_dir = self._learning_dir() / str(year)
        body = self._read_note_file(year_dir, seq)
        if body is not None:
            return body
        return self._read_note_file(year_dir / ARCHIVE_DIRNAME, seq)

    # --- CRUD operations: Notes ---

    def add_note(self, note_type: str, content: str, session_id: str,
                 summary: str = '', brief_summary: str = '',
                 **metadata) -> dict:
        """Add a new note. Returns the created index entry dict.

        ``brief_summary`` is a sidebar-friendly one-liner (<= 120 chars).
        Omit to auto-derive from ``content`` via ``derive_brief_summary``.
        """
        type_dir = self._type_dir(note_type)
        now = datetime.now(timezone.utc)
        year = now.year
        year_dir = self._ensure_year_dir(type_dir, year)
        seq = self._increment_seq(year_dir)
        timestamp = now.isoformat()
        # If no summary provided, derive it (first non-empty line, <=300 chars).
        if not summary:
            summary = derive_summary(content)
        brief = brief_summary.strip() if brief_summary else derive_brief_summary(content)
        if len(brief) > BRIEF_SUMMARY_MAX:
            brief = brief[:BRIEF_SUMMARY_MAX].rstrip()
        prefix = TYPE_PREFIXES.get(note_type, 'G')
        display_id = f"{prefix}-{year}-{seq}"
        # Tags are stored only when non-empty (spec §5.3) — popped here so an
        # empty list passed by a caller doesn't write a bare `tags: []`.
        tags = metadata.pop('tags', None)
        entry = {
            'display_id': display_id,
            'type': note_type,
            'year': year,
            'seq': seq,
            'status': 'active',
            'session': session_id,
            'timestamp': timestamp,
            'summary': summary,
            'brief_summary': brief,
            'has_body': bool(content),
            **metadata,
        }
        if tags:
            entry['tags'] = list(tags)
        self._append_index(year_dir, entry)
        self._write_note_file(year_dir, seq, content)
        return entry

    def get_note(self, note_type: str, year: int, seq: int) -> dict | None:
        """Retrieve a single note by type, year, and seq.

        Returns a dict with index metadata plus 'content' key, or None.
        """
        type_dir = self._type_dir(note_type)
        year_dir = type_dir / str(year)
        if not year_dir.exists():
            return None
        # Search active index
        for entry in self._read_index(year_dir):
            if entry.get('seq') == seq:
                content = self._read_note_file(year_dir, seq)
                result = {**entry, 'content': content}
                if content is None and entry.get('has_body'):
                    result['_warning'] = 'body_file_missing'
                return result
        # Check archive
        archive_dir = year_dir / ARCHIVE_DIRNAME
        for entry in self._read_index(archive_dir):
            if entry.get('seq') == seq:
                content = self._read_note_file(archive_dir, seq)
                # Preserve entry's own status (active/done); archived-ness
                # is indicated by '_from_archive' so callers that care can
                # distinguish without losing the completion state.
                result = {**entry, 'content': content, '_from_archive': True}
                if content is None and entry.get('has_body'):
                    result['_warning'] = 'body_file_missing'
                return result
        return None

    def list_notes(self, note_type: str | None = None,
                   status: str = 'active',
                   search: str | None = None) -> list[dict]:
        """List notes, optionally filtered by type, status, and search term.

        Returns list of index entry dicts sorted by timestamp descending.
        Scans all year subfolders under each type folder.
        """
        results: list[dict] = []
        # Determine which folders to scan
        if note_type is not None:
            folder_names = [TYPE_FOLDERS[note_type]]  # raises KeyError if invalid
        else:
            folder_names = list(TYPE_FOLDERS.values())

        for folder_name in folder_names:
            type_dir = self.state_dir / folder_name
            if not type_dir.exists():
                continue
            # Scan all year subfolders (dirs whose name is all digits)
            for child in type_dir.iterdir():
                if not child.is_dir() or not child.name.isdigit():
                    continue
                year_dir = child
                if status == 'active' or status == 'all':
                    results.extend(self._read_index(year_dir))
                if status == 'archived' or status == 'all':
                    archive_dir = year_dir / ARCHIVE_DIRNAME
                    for entry in self._read_index(archive_dir):
                        # Preserve entry's own status (active/done) so
                        # archived-done notes still report as done.
                        # Archived-ness is implicit from this code path
                        # and from the presence of 'archived_at'.
                        results.append({**entry})

        # Filter by search term — case-insensitive over metadata first
        # (display_id, summary, brief_summary, session, tags), then lazily the
        # body file only if the metadata missed. Body reads happen only on the
        # search path, never on the hot index/startup path (spec §5.6).
        if search:
            search_lower = search.lower()

            def _matches(e: dict) -> bool:
                hay = ' '.join([
                    str(e.get('display_id', '')),
                    str(e.get('summary', '')),
                    str(e.get('brief_summary', '')),
                    str(e.get('session', '') or ''),
                    ' '.join(str(t) for t in (e.get('tags') or [])),
                ]).lower()
                if search_lower in hay:
                    return True
                body = self._read_body_any(e.get('type'), e.get('year'), e.get('seq'))
                return body is not None and search_lower in body.lower()

            results = [e for e in results if _matches(e)]

        # Sort by timestamp descending
        results.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
        return results

    def find_active_with_tag(self, tag: str) -> dict | None:
        """Return the newest active note (any type) carrying *tag* exactly.

        "Active" means status ``active`` and not archived — archived or
        done/dropped/deferred notes are ignored. Backs ``--unique-tag`` and the
        Asana tool's mirror-dedup (spec §5.14). Returns None when no match.
        """
        for entry in self.list_notes(status='active'):
            if entry.get('status') == 'active' and tag in (entry.get('tags') or []):
                return entry
        return None

    def update_note(self, note_type: str, year: int, seq: int, **kwargs) -> dict | None:
        """Update fields on an existing note's index entry.

        Supports updating: summary, brief_summary, status, content (rewrites
        .md file). When ``content`` is updated without an explicit
        ``brief_summary`` kwarg, brief_summary is re-derived from the new
        content so the sidebar stays in sync. Returns the updated entry dict,
        or None if not found.
        """
        content_update = kwargs.pop('content', None)
        add_tags = kwargs.pop('add_tags', None)
        remove_tags = kwargs.pop('remove_tags', None)
        type_dir = self._type_dir(note_type)
        year_dir = type_dir / str(year)
        if not year_dir.exists():
            return None
        entries = self._read_index(year_dir)
        for i, entry in enumerate(entries):
            if entry.get('seq') == seq:
                entries[i] = {**entry, **kwargs}
                # Stamp status_changed_at on any status transition (done/drop/
                # defer/resume), but never on a content-only edit. Mirrors the
                # source scribe oracle (spec §5.6).
                if 'status' in kwargs:
                    entries[i]['status_changed_at'] = datetime.now(timezone.utc).isoformat()
                # Tag mutation: remove-all-occurrences then add-if-absent, so
                # the result is a dedup'd, order-preserving list (spec §5.14).
                if add_tags or remove_tags:
                    tags = list(entries[i].get('tags') or [])
                    if remove_tags:
                        rm = set(remove_tags)
                        tags = [t for t in tags if t not in rm]
                    for t in (add_tags or []):
                        if t not in tags:
                            tags.append(t)
                    entries[i]['tags'] = tags
                if content_update is not None:
                    self._write_note_file(year_dir, seq, content_update)
                    entries[i]['has_body'] = bool(content_update)
                    if 'brief_summary' not in kwargs:
                        entries[i]['brief_summary'] = derive_brief_summary(content_update)
                if 'brief_summary' in kwargs and len(entries[i].get('brief_summary', '')) > BRIEF_SUMMARY_MAX:
                    entries[i]['brief_summary'] = entries[i]['brief_summary'][:BRIEF_SUMMARY_MAX].rstrip()
                self._write_index(year_dir, entries)
                return entries[i]
        return None

    def archive_note(self, note_type: str, year: int, seq: int) -> dict | None:
        """Move a note to its year folder's archive.

        Moves the index entry from year_dir/index.jsonl to
        year_dir/archive/index.jsonl, and moves <seq>.md to archive/.
        Preserves the note's existing status ('done' or 'active') rather
        than clobbering it to 'archived', so completion history survives
        the move. Archived-ness is indicated by folder location and the
        added ``archived_at`` timestamp, not by the status field.
        Returns the archived entry dict, or None if not found.
        """
        type_dir = self._type_dir(note_type)
        year_dir = type_dir / str(year)
        if not year_dir.exists():
            return None
        entries = self._read_index(year_dir)
        target_entry = None
        remaining = []
        for entry in entries:
            if entry.get('seq') == seq:
                target_entry = entry
            else:
                remaining.append(entry)
        if target_entry is not None:
            # Remove from active index
            self._write_index(year_dir, remaining)
            # Add to archive index — preserve status, stamp archived_at
            archive_dir = year_dir / ARCHIVE_DIRNAME
            archive_dir.mkdir(parents=True, exist_ok=True)
            target_entry['archived_at'] = datetime.now(timezone.utc).isoformat()
            self._append_index(archive_dir, target_entry)
            # Move .md file
            src_md = year_dir / f"{seq}.md"
            dst_md = archive_dir / f"{seq}.md"
            if src_md.exists():
                dst_md.write_text(src_md.read_text(encoding='utf-8'), encoding='utf-8')
                src_md.unlink()
            return target_entry
        return None

    def unarchive_note(self, note_type: str, year: int, seq: int) -> dict | None:
        """Move a note from the archive back into the active index.

        Inverse of archive_note. Strips the archived_at timestamp, preserves the
        note's status (which was preserved on archive), moves the .md body back
        to year_dir, and appends the index entry to the active index.
        Returns the unarchived entry dict, or None if not found in the archive.
        """
        type_dir = self._type_dir(note_type)
        year_dir = type_dir / str(year)
        archive_dir = year_dir / ARCHIVE_DIRNAME
        if not archive_dir.exists():
            return None
        entries = self._read_index(archive_dir)
        target_entry = None
        remaining = []
        for entry in entries:
            if entry.get('seq') == seq:
                target_entry = entry
            else:
                remaining.append(entry)
        if target_entry is None:
            return None
        # Strip archived_at; preserve status as-is
        target_entry.pop('archived_at', None)
        self._write_index(archive_dir, remaining)
        year_dir.mkdir(parents=True, exist_ok=True)
        self._append_index(year_dir, target_entry)
        src_md = archive_dir / f"{seq}.md"
        dst_md = year_dir / f"{seq}.md"
        if src_md.exists():
            dst_md.write_text(src_md.read_text(encoding='utf-8'), encoding='utf-8')
            src_md.unlink()
        return target_entry

    # --- CRUD operations: Learnings ---

    def add_learning(self, content: str, session_id: str,
                     summary: str = '', brief_summary: str = '',
                     tags: list[str] | None = None,
                     areas: list[str] | None = None,
                     supersedes: str | None = None,
                     **metadata) -> dict:
        """Add a new learning. Returns the created index entry dict.

        ``tags`` and ``areas`` are optional lists that also get mirrored
        into the .md file's YAML-ish frontmatter (see _format_learning_content)
        so the .md is self-contained for humans and git diffs. ``supersedes``
        records the ID of a prior learning this one replaces.
        """
        learn_dir = self._learning_dir()
        now = datetime.now(timezone.utc)
        year = now.year
        year_dir = self._ensure_year_dir(learn_dir, year)
        seq = self._increment_seq(year_dir)
        timestamp = now.isoformat()
        if not summary:
            summary = derive_summary(content)
        brief = brief_summary.strip() if brief_summary else derive_brief_summary(content)
        if len(brief) > BRIEF_SUMMARY_MAX:
            brief = brief[:BRIEF_SUMMARY_MAX].rstrip()
        display_id = f"L-{year}-{seq}"
        tags_list = list(tags) if tags else []
        areas_list = list(areas) if areas else []
        entry = {
            'display_id': display_id,
            'type': 'learning',
            'year': year,
            'seq': seq,
            'status': 'active',
            'session': session_id,
            'timestamp': timestamp,
            'summary': summary,
            'brief_summary': brief,
            'has_body': bool(content),
            'tags': tags_list,
            'areas': areas_list,
            **({'supersedes': supersedes} if supersedes else {}),
            **metadata,
        }
        frontmatter = {
            'tags': tags_list,
            'areas': areas_list,
            'supersedes': supersedes,
        }
        self._append_index(year_dir, entry)
        self._write_note_file(year_dir, seq, _format_learning_content(content, frontmatter))
        return entry

    def list_learnings(self, search: str | None = None, *,
                       tag: str | None = None,
                       area: str | None = None,
                       status: str = 'active') -> list[dict]:
        """List all learnings, optionally filtered by search term, tag, area,
        and status. Scans all year subfolders under the learnings dir.

        ``status`` is one of 'active' (default), 'archived', or 'all'.
        ``tag`` matches case-insensitively against any entry in the tags list.
        ``area`` must equal one of the globs in the entry's areas list.
        """
        learn_dir = self._learning_dir()
        entries: list[dict] = []
        if learn_dir.exists():
            for child in learn_dir.iterdir():
                if not (child.is_dir() and child.name.isdigit()):
                    continue
                if status in ('active', 'all'):
                    entries.extend(self._read_index(child))
                if status in ('archived', 'all'):
                    archive_dir = child / ARCHIVE_DIRNAME
                    for entry in self._read_index(archive_dir):
                        entries.append({**entry, '_from_archive': True})
        if search:
            search_lower = search.lower()

            def _lmatches(e: dict) -> bool:
                hay = ' '.join([
                    str(e.get('summary', '')),
                    ' '.join(str(t) for t in (e.get('tags') or [])),
                ]).lower()
                if search_lower in hay:
                    return True
                body = self._read_learning_body_any(e.get('year'), e.get('seq'))
                return body is not None and search_lower in body.lower()

            entries = [e for e in entries if _lmatches(e)]
        if tag:
            tag_lower = tag.lower()
            entries = [e for e in entries
                       if any(tag_lower in str(t).lower() for t in e.get('tags', []) or [])]
        if area:
            entries = [e for e in entries if area in (e.get('areas', []) or [])]
        entries.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
        return entries

    def get_learning(self, year: int, seq: int) -> dict | None:
        """Retrieve a single learning by year and seq. Falls back to the
        archive when the learning is not found in the active index, so
        superseded entries remain reachable via display ID.

        When the .md body carries frontmatter (``tags``/``areas``/
        ``supersedes``), those fields overlay the index entry on the returned
        dict — the .md is the source of truth for frontmatter after a manual
        edit, and the index is a mirror.
        """
        learn_dir = self._learning_dir()
        year_dir = learn_dir / str(year)
        if not year_dir.exists():
            return None
        for entry in self._read_index(year_dir):
            if entry.get('seq') == seq:
                return self._build_learning_result(entry, year_dir, seq, from_archive=False)
        archive_dir = year_dir / ARCHIVE_DIRNAME
        for entry in self._read_index(archive_dir):
            if entry.get('seq') == seq:
                return self._build_learning_result(entry, archive_dir, seq, from_archive=True)
        return None

    def _build_learning_result(self, entry: dict, dir_path: Path, seq: int,
                               *, from_archive: bool) -> dict:
        raw = self._read_note_file(dir_path, seq)
        fm, body = _parse_learning_content(raw) if raw is not None else ({}, None)
        result = {**entry, 'content': body}
        for key in _LEARNING_FRONTMATTER_FIELDS:
            if key in fm:
                result[key] = fm[key]
        if from_archive:
            result['_from_archive'] = True
        if body is None and entry.get('has_body'):
            result['_warning'] = 'body_file_missing'
        return result

    def archive_learning(self, year: int, seq: int) -> dict | None:
        """Move a learning to its year folder's archive.

        Parallels ``archive_note``. Moves the index entry and the .md file
        (frontmatter included) from ``learnings/<year>/`` into
        ``learnings/<year>/archive/``, stamping ``archived_at``. Returns the
        archived entry dict, or None if the learning was not found.
        """
        learn_dir = self._learning_dir()
        year_dir = learn_dir / str(year)
        if not year_dir.exists():
            return None
        entries = self._read_index(year_dir)
        target_entry = None
        remaining: list[dict] = []
        for entry in entries:
            if entry.get('seq') == seq:
                target_entry = entry
            else:
                remaining.append(entry)
        if target_entry is None:
            return None
        self._write_index(year_dir, remaining)
        archive_dir = year_dir / ARCHIVE_DIRNAME
        archive_dir.mkdir(parents=True, exist_ok=True)
        target_entry['archived_at'] = datetime.now(timezone.utc).isoformat()
        self._append_index(archive_dir, target_entry)
        src_md = year_dir / f"{seq}.md"
        dst_md = archive_dir / f"{seq}.md"
        if src_md.exists():
            dst_md.write_text(src_md.read_text(encoding='utf-8'), encoding='utf-8')
            src_md.unlink()
        return target_entry

    def remove_learning(self, year: int, seq: int) -> dict | None:
        """Remove a learning by year and seq. Returns the removed entry or None."""
        learn_dir = self._learning_dir()
        year_dir = learn_dir / str(year)
        if not year_dir.exists():
            return None
        entries = self._read_index(year_dir)
        target = None
        remaining = []
        for entry in entries:
            if entry.get('seq') == seq:
                target = entry
            else:
                remaining.append(entry)
        if target is not None:
            self._write_index(year_dir, remaining)
            md_path = year_dir / f"{seq}.md"
            if md_path.exists():
                md_path.unlink()
            return target
        return None
