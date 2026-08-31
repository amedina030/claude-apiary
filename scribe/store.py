"""Folder-per-type storage engine for scribe v2.

Provides ScribeStore — a class that manages notes and learnings using
individual .md files organized into type folders, each with its own
index.jsonl for fast listing.
"""

import json
import re
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Repo-root import for core.utils
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import frontmatter as fm_lib
from core.utils.atomic import replace_atomic, write_text_atomic
from core.utils.filelock import FileLock

# --- Constants ---

TYPE_FOLDERS: dict[str, str] = {
    "todo": "todos",
    "handoff": "handoffs",
    "decision": "decisions",
    "wishlist": "wishlists",
    "blocker": "blockers",
    "context": "context",
    "general": "general",
    "reference": "references",
}

#: The note types ``add --type`` accepts, in the order --help lists them.
#: Same set as ``TYPE_FOLDERS``' keys; learnings are a separate store, not a
#: type. One list, so the CLI, the installer and the template scaffolder
#: cannot disagree about what a note type is.
VALID_TYPES: list[str] = [
    "todo",
    "handoff",
    "decision",
    "wishlist",
    "reference",
    "blocker",
    "context",
    "general",
]

TYPE_PREFIXES: dict[str, str] = {
    "todo": "T",
    "handoff": "H",
    "decision": "D",
    "wishlist": "W",
    "reference": "R",
    "blocker": "B",
    "context": "C",
    "general": "G",
    "learning": "L",
}

LEARNING_FOLDER = "learnings"

# All managed folder names (type folders + learnings)
_ALL_FOLDERS: list[str] = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]

INDEX_FILENAME = "index.jsonl"
NEXT_SEQ_FILENAME = "next_seq"
ARCHIVE_DIRNAME = "archive"

# Brief-summary cap — shorter than `summary` (300), aimed at one-line display
# in the GUI sidebar. Lives next to summary on each index entry.
BRIEF_SUMMARY_MAX = 120

# Fields serialized into learning .md frontmatter. Order is fixed to keep
# diffs stable across re-writes.
_LEARNING_FRONTMATTER_FIELDS = ("tags", "areas", "supersedes")


def _format_learning_content(content: str, frontmatter: dict | None = None) -> str:
    """Prefix ``content`` with a ``---`` frontmatter block when any of the
    supported fields are present. Returns ``content`` unchanged when the
    frontmatter dict is empty — so legacy learnings stay legacy-shaped.

    Field selection and order are scribe policy; the rendering is
    ``core.frontmatter``'s. ``list_style='inline'`` keeps the on-disk shape
    the 595 existing learnings already have (``tags: [a, b]``).
    """
    if not frontmatter:
        return content
    meta = {}
    for key in _LEARNING_FRONTMATTER_FIELDS:
        value = frontmatter.get(key)
        if value is None or value == [] or value == "":
            continue
        meta[key] = list(value) if isinstance(value, tuple) else value
    return fm_lib.dump(meta, content, list_style="inline")


def _parse_learning_content(text: str) -> tuple[dict, str]:
    """Split a learning .md into ``(frontmatter_dict, body)``.

    Thin wrapper over ``core.frontmatter.parse`` in tolerant mode: files
    without frontmatter return ``({}, text)`` so the legacy corpus keeps
    working, and a malformed block falls back to the same empty-fm path rather
    than raising — scribe callers on the hot PreToolUse path cannot afford to
    crash on a hand-edited .md.
    """
    return fm_lib.parse(text)


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
    s = (content or "").strip()
    if not s:
        return ""
    first_nl = s.find("\n")
    if re.match(r"^#{1,6}\s", s):
        head_line = s if first_nl == -1 else s[:first_nl]
        head = head_line.lstrip("#").strip()
        return head[:BRIEF_SUMMARY_MAX].rstrip()
    flat = re.sub(r"\s+", " ", s).strip()
    window = flat[:BRIEF_SUMMARY_MAX]
    sent = re.search(r"[.!?](?=\s|$)", window)
    if sent:
        return window[: sent.end()].rstrip()
    paren_close = window.find(")")
    if paren_close >= 30:
        return window[: paren_close + 1].rstrip()
    colon = re.search(r":(?=\s)", window)
    if colon and colon.start() >= 10:
        return window[: colon.end()].rstrip()
    # Em-dash (U+2014) or double-hyphen often separates a clause from its
    # elaboration ("X foo — does Y"); cut just before it.
    dash = re.search(r"\s[—–]\s|\s--\s", window)
    if dash and dash.start() >= 30:
        return window[: dash.start()].rstrip()
    if len(flat) <= BRIEF_SUMMARY_MAX:
        return flat
    # Last-resort deep comma cut — only if well into the brief so we don't
    # chop trivially short. Drops the trailing fragment cleanly.
    comma = window.rfind(",")
    if comma >= 60:
        return window[:comma].rstrip()
    last_space = window.rfind(" ")
    if last_space > 40:
        return window[:last_space].rstrip() + "…"
    return window.rstrip() + "…"


def derive_summary(content: str) -> str:
    """First non-empty, stripped line of *content*, truncated to 300 chars.

    The summary rule adopted from the source scribe (spec §5.6). Distinct from
    ``derive_brief_summary`` (the GUI sidebar's <=120-char heuristic), which is
    intentionally left unchanged.
    """
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:300]
    return ""


#: State dirs whose layout this process has already confirmed, keyed by
#: ``(path, year)`` — the year because a new one needs new subfolders.
_LAYOUT_CHECKED: set = set()


def reset_layout_cache() -> None:
    """Forget which layouts this process has confirmed.

    Only tests need this: a store built on a path, then that path deleted,
    then a store built on it again would otherwise skip the rebuild.
    """
    _LAYOUT_CHECKED.clear()


class _IndexTxn:
    """A folder's index rows, read under the lock that will write them back.

    Mutate :attr:`entries` and call :meth:`commit` to have the list written
    before the lock is released. Not committing writes nothing — which is
    what a lookup that found no matching row wants.
    """

    __slots__ = ("folder", "entries", "committed")

    def __init__(self, folder: Path, entries: list) -> None:
        self.folder = folder
        self.entries = entries
        self.committed = False

    def commit(self) -> None:
        self.committed = True

    def index_of(self, seq: int) -> "int | None":
        """Position of the row with *seq*, or None."""
        for i, entry in enumerate(self.entries):
            if entry.get("seq") == seq:
                return i
        return None


class ScribeStore:
    """Folder-per-type storage engine for notes and learnings.

    Initialized with a state_dir (Path). Manages folder layout,
    per-folder index.jsonl files, individual .md note files, and
    per-(type,year) sequence counters.

    **Concurrency contract.** Every read-modify-write of an index happens
    inside one :meth:`_locked_index` hold, so a whole-list rewrite cannot
    clobber a row another process appended in between (review §3 bug 4). A
    note's body is always written *before* its index row, so a crash between
    the two leaves an entry ``repair`` can rebuild rather than a row
    ``repair`` would delete.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self._ensure_layout_lazily()

    # --- Layout ---

    def _layout_is_current(self) -> bool:
        """True when this year's folders already exist for every note type.

        Nine ``is_dir`` calls, against ``ensure_layout``'s ~45 mkdir/exists/
        write calls. It matters because ``core/hooks/learnings_inject_hook``
        builds a store on every Edit, Write and Bash (review §3 bug 11).
        """
        year = str(datetime.now(timezone.utc).year)
        return all(
            (self.state_dir / name / year / ARCHIVE_DIRNAME).is_dir() for name in _ALL_FOLDERS
        )

    def _ensure_layout_lazily(self) -> None:
        """Build the layout only when something is actually missing, once."""
        key = (str(self.state_dir), datetime.now(timezone.utc).year)
        if key in _LAYOUT_CHECKED:
            return
        if not self._layout_is_current():
            self.ensure_layout()
        _LAYOUT_CHECKED.add(key)

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
                year_idx.write_text("", encoding="utf-8")
            seq_path = year_dir / NEXT_SEQ_FILENAME
            if not seq_path.exists():
                seq_path.write_text("1", encoding="utf-8")
            year_archive = year_dir / ARCHIVE_DIRNAME
            year_archive.mkdir(parents=True, exist_ok=True)
            year_archive_idx = year_archive / INDEX_FILENAME
            if not year_archive_idx.exists():
                year_archive_idx.write_text("", encoding="utf-8")

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
        for lineno, line in enumerate(idx_path.read_text(encoding="utf-8").splitlines(), 1):
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
                print(
                    f"Warning: skipping malformed index entry at {idx_path}:{lineno}",
                    file=sys.stderr,
                )
        return entries

    @staticmethod
    @contextmanager
    def _index_locks(*folders: Path):
        """Hold the FileLock on each folder's index.jsonl for the block.

        Locks are taken in path order, and duplicates collapse, so a move
        that goes active → archive and one that goes archive → active cannot
        take them in opposite orders and deadlock.
        """
        paths: list = []
        for folder in folders:
            path = Path(folder) / INDEX_FILENAME
            path.parent.mkdir(parents=True, exist_ok=True)
            if path not in paths:
                paths.append(path)
        with ExitStack() as stack:
            for path in sorted(paths, key=str):
                stack.enter_context(FileLock(path))
            yield

    @staticmethod
    @contextmanager
    def _locked_index(folder: Path):
        """Read one index under its lock, and write it back under the same hold.

        This is the fix for the lost-update race: every mutation used to read
        the index, decide, and only *then* take the lock to write the whole
        list back, so an append that landed in between was silently dropped.
        """
        folder = Path(folder)
        with ScribeStore._index_locks(folder):
            txn = _IndexTxn(folder, ScribeStore._read_index(folder))
            yield txn
            if txn.committed:
                ScribeStore._write_index_unlocked(folder, txn.entries)

    @staticmethod
    def _write_index_unlocked(type_dir: Path, entries: list[dict]) -> None:
        """Atomically overwrite a folder's index.jsonl. **Caller holds the lock.**"""
        lines = [json.dumps(e, separators=(",", ":")) for e in entries]
        write_text_atomic(type_dir / INDEX_FILENAME, "\n".join(lines) + ("\n" if lines else ""))

    @staticmethod
    def _write_index(type_dir: Path, entries: list[dict]) -> None:
        """Overwrite a folder's index.jsonl with the given entries.

        Atomic (temp + os.replace) under a FileLock. Safe on its own only
        when *entries* did not come from a read this caller has to defend —
        for read-modify-write, use :meth:`_locked_index`.
        """
        with ScribeStore._index_locks(type_dir):
            ScribeStore._write_index_unlocked(type_dir, entries)

    @staticmethod
    def _append_index_unlocked(type_dir: Path, entry: dict) -> None:
        """Append one entry to a folder's index.jsonl. **Caller holds the lock.**

        Appends to the file's existing *text* rather than to parsed rows, so
        a line this reader would have skipped as malformed is preserved
        instead of being dropped by the rewrite.
        """
        idx_path = type_dir / INDEX_FILENAME
        existing = idx_path.read_text(encoding="utf-8") if idx_path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        write_text_atomic(idx_path, existing + json.dumps(entry, separators=(",", ":")) + "\n")

    @staticmethod
    def _append_index(type_dir: Path, entry: dict) -> None:
        """Append a single entry to a folder's index.jsonl.

        Read-modify-write under a FileLock, flushed atomically (temp +
        os.replace) so a crash mid-append cannot leave a partial line.
        """
        with ScribeStore._index_locks(type_dir):
            ScribeStore._append_index_unlocked(type_dir, entry)

    # --- Per-(type,year) sequence counter ---

    def _ensure_year_dir(self, type_dir: Path, year: int) -> Path:
        """Ensure type_dir/<year> exists with index, next_seq, and archive. Returns it."""
        year_dir = type_dir / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        idx = year_dir / INDEX_FILENAME
        if not idx.exists():
            idx.write_text("", encoding="utf-8")
        seq_path = year_dir / NEXT_SEQ_FILENAME
        if not seq_path.exists():
            self._rebuild_next_seq(year_dir)
        archive = year_dir / ARCHIVE_DIRNAME
        archive.mkdir(parents=True, exist_ok=True)
        archive_idx = archive / INDEX_FILENAME
        if not archive_idx.exists():
            archive_idx.write_text("", encoding="utf-8")
        return year_dir

    def _rebuild_next_seq(self, year_dir: Path) -> int:
        """Scan year_dir's index.jsonl (and archive) for max 'seq'. Write and return next_seq.

        Uses strict index reads — a malformed line in either index raises
        RuntimeError rather than being skipped, so a corrupted index cannot
        silently undercount and produce colliding IDs on the next write.
        """
        max_seq = 0
        for entry in self._read_index(year_dir, strict=True):
            s = entry.get("seq", 0)
            if isinstance(s, int) and s > max_seq:
                max_seq = s
        archive_dir = year_dir / ARCHIVE_DIRNAME
        for entry in self._read_index(archive_dir, strict=True):
            s = entry.get("seq", 0)
            if isinstance(s, int) and s > max_seq:
                max_seq = s
        next_seq = max_seq + 1
        seq_path = year_dir / NEXT_SEQ_FILENAME
        seq_path.write_text(str(next_seq), encoding="utf-8")
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
                text = seq_path.read_text(encoding="utf-8").strip()
                try:
                    current = int(text)
                except ValueError:
                    current = self._rebuild_next_seq(year_dir)
            seq_path.write_text(str(current + 1), encoding="utf-8")
        return current

    # --- Note file helpers ---

    def _type_dir(self, note_type: str) -> Path:
        """Return the folder Path for a given note type."""
        folder_name = TYPE_FOLDERS.get(note_type)
        if folder_name is None:
            raise ValueError(
                f"Unknown note type: {note_type!r}. Valid types: {list(TYPE_FOLDERS.keys())}"
            )
        return self.state_dir / folder_name

    def _learning_dir(self) -> Path:
        """Return the folder Path for learnings."""
        return self.state_dir / LEARNING_FOLDER

    @staticmethod
    def _write_note_file(type_dir: Path, note_id: int, content: str) -> None:
        """Write note content to type_dir/<id>.md. Pure content, no frontmatter."""
        md_path = type_dir / f"{note_id}.md"
        md_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _read_note_file(type_dir: Path, note_id: int) -> str | None:
        """Read note content from type_dir/<id>.md. Returns None if missing."""
        md_path = type_dir / f"{note_id}.md"
        if not md_path.exists():
            return None
        return md_path.read_text(encoding="utf-8")

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

    def add_note(
        self,
        note_type: str,
        content: str,
        session_id: str,
        summary: str = "",
        brief_summary: str = "",
        **metadata,
    ) -> dict:
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
        prefix = TYPE_PREFIXES.get(note_type, "G")
        display_id = f"{prefix}-{year}-{seq}"
        # Tags are stored only when non-empty (spec §5.3) — popped here so an
        # empty list passed by a caller doesn't write a bare `tags: []`.
        tags = metadata.pop("tags", None)
        entry = {
            "display_id": display_id,
            "type": note_type,
            "year": year,
            "seq": seq,
            "status": "active",
            "session": session_id,
            "timestamp": timestamp,
            "summary": summary,
            "brief_summary": brief,
            "has_body": bool(content),
            **metadata,
        }
        if tags:
            entry["tags"] = list(tags)
        # Body first, then the row. A crash in between leaves a <seq>.md with
        # no index entry, which `repair` rebuilds; the old order left a row
        # with no body, which `repair` deletes (review §3 bug 6).
        self._write_note_file(year_dir, seq, content)
        self._append_index(year_dir, entry)
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
            if entry.get("seq") == seq:
                content = self._read_note_file(year_dir, seq)
                result = {**entry, "content": content}
                if content is None and entry.get("has_body"):
                    result["_warning"] = "body_file_missing"
                return result
        # Check archive
        archive_dir = year_dir / ARCHIVE_DIRNAME
        for entry in self._read_index(archive_dir):
            if entry.get("seq") == seq:
                content = self._read_note_file(archive_dir, seq)
                # Preserve entry's own status (active/done); archived-ness
                # is indicated by '_from_archive' so callers that care can
                # distinguish without losing the completion state.
                result = {**entry, "content": content, "_from_archive": True}
                if content is None and entry.get("has_body"):
                    result["_warning"] = "body_file_missing"
                return result
        return None

    def list_notes(
        self, note_type: str | None = None, status: str = "active", search: str | None = None
    ) -> list[dict]:
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
                if status == "active" or status == "all":
                    results.extend(self._read_index(year_dir))
                if status == "archived" or status == "all":
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
                hay = " ".join(
                    [
                        str(e.get("display_id", "")),
                        str(e.get("summary", "")),
                        str(e.get("brief_summary", "")),
                        str(e.get("session", "") or ""),
                        " ".join(str(t) for t in (e.get("tags") or [])),
                    ]
                ).lower()
                if search_lower in hay:
                    return True
                body = self._read_body_any(e.get("type"), e.get("year"), e.get("seq"))
                return body is not None and search_lower in body.lower()

            results = [e for e in results if _matches(e)]

        # Sort by timestamp descending
        results.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return results

    def find_active_with_tag(self, tag: str) -> dict | None:
        """Return the newest active note (any type) carrying *tag* exactly.

        "Active" means status ``active`` and not archived — archived or
        done/dropped/deferred notes are ignored. Backs ``--unique-tag`` and the
        Asana tool's mirror-dedup (spec §5.14). Returns None when no match.
        """
        for entry in self.list_notes(status="active"):
            if entry.get("status") == "active" and tag in (entry.get("tags") or []):
                return entry
        return None

    def update_note(self, note_type: str, year: int, seq: int, **kwargs) -> dict | None:
        """Update fields on an existing note's index entry.

        Supports updating: summary, brief_summary, status, content (rewrites
        .md file). When ``content`` is updated without an explicit
        ``brief_summary`` kwarg, brief_summary is re-derived from the new
        content so the sidebar stays in sync. Returns the updated entry dict,
        or None if not found.

        On a status change, ``status_changed_at`` is stamped with the current
        UTC time unless the caller passes a non-empty ``status_changed_at`` of
        its own, which is stored verbatim and never validated; ``None`` or
        ``""`` count as not supplied and still get the fresh stamp.

        Archive-aware: the active index is searched first, then the year's
        ``archive/`` index. An archived note is updated in place (the body
        stays in ``archive/<seq>.md``) and the returned dict carries
        ``_from_archive: True``, mirroring :meth:`get_note`. Without this,
        every ``done``/``drop``/``defer``/``resume``/``update`` on an
        auto-archived note reported success and changed nothing.
        """
        content_update = kwargs.pop("content", None)
        add_tags = kwargs.pop("add_tags", None)
        remove_tags = kwargs.pop("remove_tags", None)
        type_dir = self._type_dir(note_type)
        year_dir = type_dir / str(year)
        if not year_dir.exists():
            return None
        updated = self._update_index_entry(
            year_dir, seq, kwargs, content_update, add_tags, remove_tags
        )
        if updated is not None:
            return updated
        archive_dir = year_dir / ARCHIVE_DIRNAME
        if not archive_dir.exists():
            return None
        updated = self._update_index_entry(
            archive_dir, seq, kwargs, content_update, add_tags, remove_tags
        )
        if updated is None:
            return None
        return {**updated, "_from_archive": True}

    def _update_index_entry(
        self,
        folder: Path,
        seq: int,
        kwargs: dict,
        content_update: str | None,
        add_tags: list | None,
        remove_tags: list | None,
    ) -> dict | None:
        """Apply an update to the entry with *seq* in *folder*'s index.

        *folder* is either a year dir or that year's ``archive/`` dir — the
        body file lives beside the index in both cases. Returns the updated
        entry, or None when *folder* holds no such entry.

        The whole read-decide-write runs inside one lock hold, so a row
        appended by another process between the read and the write survives.
        """
        with self._locked_index(folder) as txn:
            i = txn.index_of(seq)
            if i is None:
                return None
            entry = {**txn.entries[i], **kwargs}
            # Stamp status_changed_at on any status transition (done/drop/
            # defer/resume), but never on a content-only edit. Mirrors the
            # source scribe oracle (spec §5.6). An explicit status_changed_at
            # wins — the same "explicit kwarg beats the derived value" rule
            # brief_summary follows below — so an API caller that knows the
            # real close time records it in one write. A missing, None or
            # empty value counts as not supplied and still gets the stamp
            # policy.done_at depends on.
            if "status" in kwargs and not kwargs.get("status_changed_at"):
                entry["status_changed_at"] = datetime.now(timezone.utc).isoformat()
            # Tag mutation: remove-all-occurrences then add-if-absent, so
            # the result is a dedup'd, order-preserving list (spec §5.14).
            if add_tags or remove_tags:
                tags = list(entry.get("tags") or [])
                if remove_tags:
                    rm = set(remove_tags)
                    tags = [t for t in tags if t not in rm]
                for t in add_tags or []:
                    if t not in tags:
                        tags.append(t)
                entry["tags"] = tags
            if content_update is not None:
                self._write_note_file(folder, seq, content_update)
                entry["has_body"] = bool(content_update)
                if "brief_summary" not in kwargs:
                    entry["brief_summary"] = derive_brief_summary(content_update)
            if (
                "brief_summary" in kwargs
                and len(entry.get("brief_summary", "")) > BRIEF_SUMMARY_MAX
            ):
                entry["brief_summary"] = entry["brief_summary"][:BRIEF_SUMMARY_MAX].rstrip()
            txn.entries[i] = entry
            txn.commit()
            return entry

    def archive_note(self, note_type: str, year: int, seq: int) -> dict | None:
        """Move a note to its year folder's archive.

        Moves the index entry from year_dir/index.jsonl to
        year_dir/archive/index.jsonl, and moves <seq>.md to archive/.
        Preserves the note's existing status ('done' or 'active') rather
        than clobbering it to 'archived', so completion history survives
        the move. Archived-ness is indicated by folder location and the
        added ``archived_at`` timestamp, not by the status field.
        Returns the archived entry dict, or None if not found.

        Both indexes are locked for the whole move, and the destination row
        is written first: a crash mid-move then leaves the note listed in
        both indexes (which ``repair`` reconciles) rather than in neither.
        The body moves with ``os.replace``, not copy-then-delete, so it
        cannot exist twice.
        """
        type_dir = self._type_dir(note_type)
        year_dir = type_dir / str(year)
        if not year_dir.exists():
            return None
        archive_dir = year_dir / ARCHIVE_DIRNAME
        return self._move_between_indexes(
            year_dir,
            archive_dir,
            seq,
            stamp={"archived_at": datetime.now(timezone.utc).isoformat()},
        )

    def _move_between_indexes(
        self, src_dir: Path, dst_dir: Path, seq: int, *, stamp: dict | None = None, drop: tuple = ()
    ) -> dict | None:
        """Move one note's row and body from *src_dir* to *dst_dir*.

        Shared by archive/unarchive for notes and learnings — four copies of
        this eleven-line dance existed, and three of them read the source
        index outside the lock. *stamp* is merged into the row, *drop* names
        keys to remove from it.
        """
        with self._index_locks(src_dir, dst_dir):
            entries = self._read_index(src_dir)
            target = next((e for e in entries if e.get("seq") == seq), None)
            if target is None:
                return None
            for key in drop:
                target.pop(key, None)
            target.update(stamp or {})
            # Destination row, then body, then remove the source row.
            self._append_index_unlocked(dst_dir, target)
            src_md = src_dir / f"{seq}.md"
            if src_md.exists():
                replace_atomic(src_md, dst_dir / f"{seq}.md")
            self._write_index_unlocked(src_dir, [e for e in entries if e.get("seq") != seq])
            return target

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
        return self._move_between_indexes(archive_dir, year_dir, seq, drop=("archived_at",))

    # --- CRUD operations: Learnings ---

    def add_learning(
        self,
        content: str,
        session_id: str,
        summary: str = "",
        brief_summary: str = "",
        tags: list[str] | None = None,
        areas: list[str] | None = None,
        supersedes: str | None = None,
        **metadata,
    ) -> dict:
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
            "display_id": display_id,
            "type": "learning",
            "year": year,
            "seq": seq,
            "status": "active",
            "session": session_id,
            "timestamp": timestamp,
            "summary": summary,
            "brief_summary": brief,
            "has_body": bool(content),
            "tags": tags_list,
            "areas": areas_list,
            **({"supersedes": supersedes} if supersedes else {}),
            **metadata,
        }
        frontmatter = {
            "tags": tags_list,
            "areas": areas_list,
            "supersedes": supersedes,
        }
        # Body before row, as in add_note.
        self._write_note_file(year_dir, seq, _format_learning_content(content, frontmatter))
        self._append_index(year_dir, entry)
        return entry

    def list_learnings(
        self,
        search: str | None = None,
        *,
        tag: str | None = None,
        area: str | None = None,
        status: str = "active",
    ) -> list[dict]:
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
                if status in ("active", "all"):
                    entries.extend(self._read_index(child))
                if status in ("archived", "all"):
                    archive_dir = child / ARCHIVE_DIRNAME
                    for entry in self._read_index(archive_dir):
                        entries.append({**entry, "_from_archive": True})
        if search:
            search_lower = search.lower()

            def _lmatches(e: dict) -> bool:
                hay = " ".join(
                    [
                        str(e.get("summary", "")),
                        " ".join(str(t) for t in (e.get("tags") or [])),
                    ]
                ).lower()
                if search_lower in hay:
                    return True
                body = self._read_learning_body_any(e.get("year"), e.get("seq"))
                return body is not None and search_lower in body.lower()

            entries = [e for e in entries if _lmatches(e)]
        if tag:
            tag_lower = tag.lower()
            entries = [
                e
                for e in entries
                if any(tag_lower in str(t).lower() for t in e.get("tags", []) or [])
            ]
        if area:
            entries = [e for e in entries if area in (e.get("areas", []) or [])]
        entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
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
            if entry.get("seq") == seq:
                return self._build_learning_result(entry, year_dir, seq, from_archive=False)
        archive_dir = year_dir / ARCHIVE_DIRNAME
        for entry in self._read_index(archive_dir):
            if entry.get("seq") == seq:
                return self._build_learning_result(entry, archive_dir, seq, from_archive=True)
        return None

    def _build_learning_result(
        self, entry: dict, dir_path: Path, seq: int, *, from_archive: bool
    ) -> dict:
        raw = self._read_note_file(dir_path, seq)
        fm, body = _parse_learning_content(raw) if raw is not None else ({}, None)
        result = {**entry, "content": body}
        for key in _LEARNING_FRONTMATTER_FIELDS:
            if key in fm:
                result[key] = fm[key]
        if from_archive:
            result["_from_archive"] = True
        if body is None and entry.get("has_body"):
            result["_warning"] = "body_file_missing"
        return result

    def update_learning(
        self, year: int, seq: int, *, tags: list | None = None, areas: list | None = None
    ) -> dict | None:
        """Replace a learning's tags/areas in the index row *and* the .md.

        Both, because ``get_learning`` treats the .md's frontmatter as the
        source of truth and the index as a mirror — writing only one of them
        leaves a learning whose listed tags and stored tags disagree. Returns
        the updated entry, or None when the learning is not in the active
        index (an archived learning is not retagged).
        """
        year_dir = self._learning_dir() / str(year)
        if not year_dir.exists():
            return None
        with self._locked_index(year_dir) as txn:
            i = txn.index_of(seq)
            if i is None:
                return None
            updated = {**txn.entries[i]}
            if tags is not None:
                updated["tags"] = list(tags)
            if areas is not None:
                updated["areas"] = list(areas)
            raw = self._read_note_file(year_dir, seq)
            fm, body = _parse_learning_content(raw) if raw is not None else ({}, "")
            self._write_note_file(
                year_dir,
                seq,
                _format_learning_content(
                    body or "",
                    {
                        "tags": updated.get("tags") or [],
                        "areas": updated.get("areas") or [],
                        "supersedes": updated.get("supersedes") or fm.get("supersedes"),
                    },
                ),
            )
            txn.entries[i] = updated
            txn.commit()
            return updated

    def archive_learning(self, year: int, seq: int) -> dict | None:
        """Move a learning to its year folder's archive.

        Parallels ``archive_note``. Moves the index entry and the .md file
        (frontmatter included) from ``learnings/<year>/`` into
        ``learnings/<year>/archive/``, stamping ``archived_at``. Returns the
        archived entry dict, or None if the learning was not found.
        """
        year_dir = self._learning_dir() / str(year)
        if not year_dir.exists():
            return None
        return self._move_between_indexes(
            year_dir,
            year_dir / ARCHIVE_DIRNAME,
            seq,
            stamp={"archived_at": datetime.now(timezone.utc).isoformat()},
        )

    def remove_learning(self, year: int, seq: int) -> dict | None:
        """Remove a learning by year and seq. Returns the removed entry or None.

        The row goes before the body, so a crash between them leaves an
        orphaned ``<seq>.md`` that ``repair`` restores — for a hard delete,
        coming back is the more recoverable failure.
        """
        year_dir = self._learning_dir() / str(year)
        if not year_dir.exists():
            return None
        with self._locked_index(year_dir) as txn:
            i = txn.index_of(seq)
            if i is None:
                return None
            target = txn.entries.pop(i)
            txn.commit()
        md_path = year_dir / f"{seq}.md"
        if md_path.exists():
            md_path.unlink()
        return target
