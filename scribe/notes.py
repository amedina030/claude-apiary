#!/usr/bin/env python3
"""
Scribe — structured note management for LLM session continuity.

Storage: JSONL files (.claude/notes.jsonl active, .claude/notes_archive.jsonl archived).
Notes are operational state — deferred work, handoffs, decisions — not permanent facts.

Requires PYTHONUTF8=1 environment variable on Windows (set by setup.py).

Usage:
    notes.py add --type todo --content "..." --session-id X [--auto] [--role X] [--mission X]
    notes.py list [--type X] [--session X] [--search X] [--last N | --limit N] [--all] [--archive] [--role X] [--mission X]
    notes.py learn --content "..." [--session-id X] [--role X] [--mission X]
    notes.py learnings [--search X] [--role X] [--mission X]
    notes.py get <id>
    notes.py done <id>
    notes.py update <id> --content "..."
    notes.py archive [--before YYYY-MM-DD]
    notes.py migrate <notes.md path>
    notes.py repair [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path as _PathImport

# Add project root to path for core.utils import
sys.path.insert(0, str(_PathImport(__file__).resolve().parent.parent))
from core.session import SessionId
from scribe.store import ScribeStore, TYPE_FOLDERS, TYPE_PREFIXES, LEARNING_FOLDER, INDEX_FILENAME, ARCHIVE_DIRNAME, NEXT_SEQ_FILENAME

from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# In-repo state layout (decision #269, todos #262–#268).
# Scribe reads/writes its state from <git-repo-root>/.apiary/scribe/ under
# the umbrella .apiary/ directory shared with other apiary tools. The
# APIARY_STATE_LAYOUT=legacy environment variable is an escape hatch that
# falls back to the historical ~/.claude/projects/<project_key>/ path.
# Default (env unset) is the in-repo layout as of todo #268.
STATE_LAYOUT_ENV = "APIARY_STATE_LAYOUT"
APIARY_STATE_DIRNAME = ".apiary"
SCRIBE_SUBDIR = "scribe"

VALID_TYPES = ["todo", "handoff", "decision", "wishlist", "reference", "blocker", "context", "general"]

_PREFIX_TO_TYPE: dict[str, str] = {
    'T': 'todo', 'H': 'handoff', 'D': 'decision', 'W': 'wishlist',
    'R': 'reference', 'B': 'blocker', 'C': 'context', 'G': 'general', 'L': 'learning',
}

AUTO_ARCHIVE_DAYS = 30
MAX_CONTENT_LENGTH = 100_000  # bytes; prevents runaway JSONL file growth
MAX_SUMMARY_LENGTH = 300  # chars; keeps index.jsonl lines small and startup injection cheap
MAX_LAST = 10_000  # upper bound for --last to prevent misleading output


def _load_session_identity():
    """Load role/mission/session_id from the most recent session identity file.

    Returns empty strings for all fields if the identity file is unavailable
    or malformed — this keeps the CLI usable in environments without a valid
    session identity.
    """
    try:
        from core.session import load_identity
        identity = load_identity()
        return identity.get("role", ""), identity.get("mission", ""), identity.get("session_id", "")
    except Exception:
        return "", "", ""


from core.utils.project import get_project_key  # moved to core; re-exported


_PROJECT_KEY_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,200}$')


def _project_key(project_override=None):
    """Return the project key, from --project flag or cwd."""
    if project_override:
        # Reject values that could escape PROJECTS_DIR via traversal components.
        if not _PROJECT_KEY_RE.match(project_override):
            print(
                f"Error: --project value contains invalid characters: {project_override!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        # Double-check the resolved path stays inside PROJECTS_DIR.
        resolved = (PROJECTS_DIR / project_override).resolve()
        if not str(resolved).startswith(str(PROJECTS_DIR.resolve())):
            print(
                f"Error: --project value escapes projects directory: {project_override!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        return project_override
    return get_project_key(Path.cwd())


def _use_repo_layout() -> bool:
    """Return True when the repo-root state layout is active.

    Default is the in-repo layout under ``<git-repo-root>/.apiary/scribe/``
    (flipped in todo #268). Set ``APIARY_STATE_LAYOUT=legacy``
    (case-insensitive) as an escape hatch to fall back to the historical
    ``~/.claude/projects/<project_key>/`` location.
    """
    return os.environ.get(STATE_LAYOUT_ENV, "").strip().lower() != "legacy"


def _git_repo_root(start: Path | None = None) -> Path | None:
    """Return the git repo root containing *start* (or cwd), or None.

    Uses ``git rev-parse --show-toplevel`` via list-form subprocess for
    portability. Returns None when git is unavailable, when *start* is not
    inside a repo, or when the command fails for any other reason.
    """
    cwd = str(start) if start is not None else str(Path.cwd())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    return Path(top)


def _repo_scribe_dir(start: Path | None = None) -> Path:
    """Return the in-repo scribe state directory for the new layout.

    Resolves to ``<git-repo-root>/.apiary/scribe/`` when *start* (or cwd) is
    inside a git repo, and falls back to ``<cwd>/.apiary/scribe/`` when it
    is not. The caller is responsible for ensuring APIARY_STATE_LAYOUT=repo
    before using this; see _use_repo_layout().
    """
    root = _git_repo_root(start) or (start or Path.cwd())
    return Path(root) / APIARY_STATE_DIRNAME / SCRIBE_SUBDIR


def scribe_state_dir(start: Path | None = None) -> Path | None:
    """Return the scribe state *directory* under the active layout.

    Repo layout (APIARY_STATE_LAYOUT=repo): resolves ``<git-repo-root>/.apiary/scribe/``
    via git rev-parse run from *start* (or cwd). Returns ``None`` when *start*
    is not inside a git repo — callers must decide whether to fall back to the
    legacy path, skip state loading, or error.

    Legacy layout: returns ``None``. Legacy callers use the project-key helpers
    below instead of the state-dir concept.
    """
    if not _use_repo_layout():
        return None
    root = _git_repo_root(start)
    if root is None:
        return None
    return root / APIARY_STATE_DIRNAME / SCRIBE_SUBDIR


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def format_age(ts):
    """Return human-readable relative age string from an ISO timestamp."""
    dt = _parse_timestamp(ts)
    if dt is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    delta = now - dt

    total_seconds = delta.total_seconds()
    if total_seconds < 0:
        return "in the future"
    minutes = int(total_seconds / 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    return f"{months}mo ago"


def _run_auto_archive_store(store) -> int:
    """Run auto-archive using ScribeStore. Returns count of archived notes.

    Retention rules by type:
      handoff  - keep only the latest per role/mission, archive the rest
      context  - archive after 3 days (mid-session checkpoints decay fast)
      decision - archive after 30 days (historical record, not live state)
      done     - archive after 1 day (any type marked done)
      todo/wishlist/blocker - keep until done
    """
    now = datetime.now(timezone.utc)
    context_cutoff = now - timedelta(days=3)
    decision_cutoff = now - timedelta(days=30)
    done_cutoff = now - timedelta(days=1)

    all_notes = store.list_notes(status='active')

    # Find the latest handoff per role/mission
    latest_handoff = {}  # (role, mission) -> newest timestamp
    for n in all_notes:
        if n.get('type') == 'handoff':
            key = (n.get('role', 'user'), n.get('mission', 'general'))
            ts = _parse_timestamp(n.get('timestamp', ''))
            if ts is not None:
                if key not in latest_handoff or ts > latest_handoff[key]:
                    latest_handoff[key] = ts

    to_archive_ids = []
    for n in all_notes:
        ts = _parse_timestamp(n.get('timestamp', ''))
        ntype = n.get('type', '')
        if ts is None:
            continue
        if n.get('status') == 'done' and ts < done_cutoff:
            to_archive_ids.append((n['type'], n['year'], n['seq']))
        elif ntype == 'handoff':
            key = (n.get('role', 'user'), n.get('mission', 'general'))
            if ts < latest_handoff.get(key, ts):
                to_archive_ids.append((n['type'], n['year'], n['seq']))
        elif ntype == 'context' and ts < context_cutoff:
            to_archive_ids.append((n['type'], n['year'], n['seq']))
        elif ntype == 'decision' and ts < decision_cutoff:
            to_archive_ids.append((n['type'], n['year'], n['seq']))

    for ntype, nyear, nseq in to_archive_ids:
        store.archive_note(ntype, nyear, nseq)
    return len(to_archive_ids)


def run_auto_archive(project_key: str, *, start: Path | None = None) -> int:
    """Run auto-archive for a project. Returns count of archived notes."""
    sd = scribe_state_dir(start)
    if sd is None:
        sd = PROJECTS_DIR / project_key
    store = ScribeStore(sd)
    return _run_auto_archive_store(store)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(args):
    store = args.store

    # Duplicate handoff prevention
    if getattr(args, 'if_no_handoff_for', None):
        try:
            target_sid = SessionId(args.if_no_handoff_for)
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            return
        # Search existing handoffs in store — include archived, because the
        # auto-archive rule "keep only the latest handoff per role/mission"
        # moves prior handoffs to the archive almost immediately. Without
        # scanning archived, the idempotency guard misses them.
        handoffs = store.list_notes(note_type='handoff', status='all')
        for h in handoffs:
            if target_sid.matches(h.get('session', '')):
                print(f"Handoff for session {target_sid.short} already exists ({h.get('display_id', _format_id(h))}). Skipping.")
                return

    content = args.content
    if len(content.encode('utf-8')) > MAX_CONTENT_LENGTH:
        print(f'Error: content exceeds {MAX_CONTENT_LENGTH} bytes', file=sys.stderr)
        sys.exit(1)

    summary = (getattr(args, 'summary', '') or '').strip()
    if args.type == 'handoff' and not summary:
        print(
            'Error: --summary is required for --type handoff. '
            'Provide a one-line abstract (e.g. "Session abc12345: fixed X, decided Y").',
            file=sys.stderr,
        )
        sys.exit(1)
    if len(summary) > MAX_SUMMARY_LENGTH:
        print(
            f'Error: --summary exceeds {MAX_SUMMARY_LENGTH} chars ({len(summary)}). '
            'Keep it to a one-line abstract.',
            file=sys.stderr,
        )
        sys.exit(1)

    # Build metadata dict for extra fields
    metadata = {}
    if getattr(args, 'auto', False):
        metadata['auto_generated'] = True
    if getattr(args, 'role', ''):
        metadata['role'] = args.role
    if getattr(args, 'mission', ''):
        metadata['mission'] = args.mission

    entry = store.add_note(
        note_type=args.type,
        content=content,
        session_id=args.session_id or '',
        summary=summary,
        **metadata,
    )
    print(f"Added {_format_id(entry)} ({entry['type']})")

    # Run auto-archive after add
    _run_auto_archive_store(store)


def cmd_list(args):
    store = args.store

    # Determine status filter
    if args.archive:
        status = 'archived'
        source = 'archive'
    else:
        status = 'all' if args.all else 'active'
        source = 'active'

    # Run auto-archive before listing active notes (only for unfiltered queries)
    if not args.archive and not args.search and not args.type and not args.session and not args.role and not args.mission:
        archived_count = _run_auto_archive_store(store)
        if archived_count:
            print(f'[auto-archived {archived_count} notes]', file=sys.stderr)

    # Get notes from store
    note_type = args.type if args.type else None
    notes_list = store.list_notes(note_type=note_type, status=status, search=args.search)

    # --deferred shows only deferred notes; takes precedence over default hiding.
    if getattr(args, 'deferred', False):
        notes_list = [n for n in notes_list if n.get('status') == 'deferred']
    elif not args.all and not args.archive:
        # Default active view hides done/dropped/deferred. --all includes all three.
        notes_list = [n for n in notes_list if n.get('status') not in ('done', 'dropped', 'deferred')]

    # Filter by session
    if args.session:
        try:
            sid = SessionId(args.session)
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            return
        notes_list = [n for n in notes_list if sid.matches(n.get('session', ''))]

    # Filter by role
    if args.role:
        notes_list = [n for n in notes_list if n.get('role', '').lower() == args.role.lower()]

    # Filter by mission
    if args.mission:
        notes_list = [n for n in notes_list if n.get('mission', '').lower() == args.mission.lower()]

    # Limit
    if args.last is not None:
        if args.last <= 0:
            print('Error: --last must be a positive integer', file=sys.stderr)
            sys.exit(1)
        last = min(args.last, MAX_LAST)
        notes_list = notes_list[-last:]

    if not notes_list:
        print(f'No {source} notes found.')
        return

    for n in notes_list:
        ntype = n.get('type', '?')[:8]
        age = format_age(n.get('timestamp', ''))
        st = n.get('status', '')
        status_label = f' [{st.upper()}]' if st in ('done', 'dropped', 'deferred') else ''
        # For store entries, content is in 'summary' field; read full content for display
        content = n.get('summary', '').replace('\n', ' ')[:80]
        line = f'{_format_id(n):<12} {ntype:<10} ({age:<9}) {content}{status_label}'
        print(line.encode('ascii', errors='replace').decode('ascii'))


def _format_id(entry: dict) -> str:
    """Return the display ID string for a note/learning entry."""
    prefix = TYPE_PREFIXES.get(entry.get('type', ''), '?')
    return f"{prefix}-{entry.get('year', '?')}-{entry.get('seq', '?')}"


def _parse_display_id(display_id: str) -> tuple:
    """Parse a display ID string like 'T-2026-1' into (note_type, year, seq)."""
    m = re.match(r'^([A-Z])-([0-9]{4})-([0-9]+)$', display_id.upper())
    if not m:
        raise ValueError(f'Bad display_id: {display_id}')
    prefix = m.group(1)
    note_type = _PREFIX_TO_TYPE.get(prefix)
    if note_type is None:
        raise ValueError(f'Unknown prefix {prefix!r} in display_id: {display_id}')
    return (note_type, int(m.group(2)), int(m.group(3)))


def _load_migration_map(store) -> dict:
    """Load the migration ID map (old int ID -> display ID string). Returns {} if missing."""
    path = store.state_dir / 'migration_id_map.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def _parse_id_arg(raw: str, store) -> tuple:
    """Parse an ID argument. Returns (note_type, year, seq).

    Accepts:
      - TYPE-YEAR-seq format: e.g. T-2026-1, L-2026-3
      - Legacy bare integer: e.g. 42 (looked up in migration_id_map.json)
      - Legacy L-prefix integer: e.g. L3 (looked up in migration_id_map.json)
    """
    raw = raw.strip()
    # Try TYPE-YEAR-seq format: e.g. T-2026-1
    m = re.match(r'^([A-Z])-([0-9]{4})-([0-9]+)$', raw.upper())
    if m:
        prefix, year_str, seq_str = m.group(1), m.group(2), m.group(3)
        if prefix not in _PREFIX_TO_TYPE:
            print(f"Error: invalid ID: {raw}", file=sys.stderr)
            sys.exit(1)
        return (_PREFIX_TO_TYPE[prefix], int(year_str), int(seq_str))

    # Legacy: try bare integer or L<n> via migration_id_map.json
    migration_map = _load_migration_map(store)
    if raw.upper().startswith('L'):
        try:
            old_int_val = int(raw[1:])
        except ValueError:
            print(f"Error: invalid ID: {raw}", file=sys.stderr)
            sys.exit(1)
        if old_int_val <= 0:
            print(f"Error: invalid ID: {raw}", file=sys.stderr)
            sys.exit(1)
        old_int = str(old_int_val)
        mapped = migration_map.get(old_int)
        if mapped:
            return _parse_display_id(mapped)
        print(f"Error: invalid ID: {raw}", file=sys.stderr)
        sys.exit(1)
    try:
        old_int = str(int(raw))
    except ValueError:
        print(f"Error: invalid ID: {raw}", file=sys.stderr)
        sys.exit(1)
    mapped = migration_map.get(old_int)
    if mapped:
        return _parse_display_id(mapped)
    print(f"Error: invalid ID: {raw}", file=sys.stderr)
    sys.exit(1)


def cmd_get(args):
    store = args.store
    note_type, year, seq = _parse_id_arg(args.id, store)
    source_label = None
    is_learning = False

    if note_type == 'learning':
        note = store.get_learning(year, seq)
        is_learning = True
    else:
        note = store.get_note(note_type, year, seq)
        if note and note.get('_from_archive'):
            source_label = '[from archive]'

    if source_label:
        print(source_label)

    if not note:
        print(f'Note {args.id} not found.', file=sys.stderr)
        sys.exit(1)

    print(f"ID: {_format_id(note)}")
    if is_learning:
        print('Type: learning')
    else:
        print(f"Type: {note.get('type', '?')}")
        print(f"Status: {note.get('status', '?')}")
    print(f"Session: {note.get('session', '?')}")
    print(f"Time: {note.get('timestamp', '?')} ({format_age(note.get('timestamp', ''))})")
    if note.get('role'):
        print(f"Role: {note['role']}")
    if note.get('mission'):
        print(f"Mission: {note['mission']}")
    if not is_learning:
        print(f"Auto: {note.get('auto_generated', False)}")
    print('---')
    print(note.get('content', ''))


def cmd_done(args):
    store = args.store
    note_type, year, seq = _parse_id_arg(str(args.id), store)
    if note_type == 'learning':
        print(f'Error: {args.id} is a learning — use "unlearn" to remove it.', file=sys.stderr)
        sys.exit(1)
    note = store.get_note(note_type, year, seq)
    if not note:
        print(f'Note {args.id} not found.', file=sys.stderr)
        sys.exit(1)
    if note.get('status') == 'done':
        print(f'Note {args.id} is already marked done.')
        return
    store.update_note(note_type, year, seq, status='done')
    print(f'Marked {args.id} as done.')


def cmd_drop(args):
    store = args.store
    note_type, year, seq = _parse_id_arg(str(args.id), store)
    if note_type == 'learning':
        print(f'Error: {args.id} is a learning — use "unlearn" to remove it.', file=sys.stderr)
        sys.exit(1)
    note = store.get_note(note_type, year, seq)
    if not note:
        print(f'Note {args.id} not found.', file=sys.stderr)
        sys.exit(1)
    if note.get('status') == 'dropped':
        print(f'Note {args.id} is already dropped.')
        return
    if note.get('status') == 'done':
        print(f'Error: note {args.id} is already done; cannot drop.', file=sys.stderr)
        sys.exit(1)
    store.update_note(note_type, year, seq, status='dropped')
    print(f'Marked {args.id} as dropped.')


def cmd_defer(args):
    store = args.store
    note_type, year, seq = _parse_id_arg(str(args.id), store)
    if note_type == 'learning':
        print(f'Error: {args.id} is a learning — use "unlearn" to remove it.', file=sys.stderr)
        sys.exit(1)
    note = store.get_note(note_type, year, seq)
    if not note:
        print(f'Note {args.id} not found.', file=sys.stderr)
        sys.exit(1)
    if note.get('status') == 'deferred':
        print(f'Note {args.id} is already deferred.')
        return
    if note.get('status') in ('done', 'dropped'):
        print(
            f'Error: note {args.id} is {note.get("status")}; cannot defer a closed note.',
            file=sys.stderr,
        )
        sys.exit(1)
    store.update_note(note_type, year, seq, status='deferred')
    print(f'Deferred {args.id}. Use "resume {args.id}" to bring it back.')


def cmd_resume(args):
    store = args.store
    note_type, year, seq = _parse_id_arg(str(args.id), store)
    if note_type == 'learning':
        print(f'Error: {args.id} is a learning and cannot be resumed.', file=sys.stderr)
        sys.exit(1)
    note = store.get_note(note_type, year, seq)
    if not note:
        print(f'Note {args.id} not found.', file=sys.stderr)
        sys.exit(1)
    if note.get('status') != 'deferred':
        print(f'Note {args.id} is not deferred (status: {note.get("status", "?")}).')
        return
    store.update_note(note_type, year, seq, status='active')
    print(f'Resumed {args.id}.')


def cmd_unarchive(args):
    store = args.store
    note_type, year, seq = _parse_id_arg(str(args.id), store)
    if note_type == 'learning':
        print(f'Error: {args.id} is a learning and does not use archive.', file=sys.stderr)
        sys.exit(1)
    entry = store.unarchive_note(note_type, year, seq)
    if entry is None:
        print(f'Note {args.id} not found in archive.', file=sys.stderr)
        sys.exit(1)
    print(f'Unarchived {args.id} (status preserved: {entry.get("status", "?")}).')


def cmd_update(args):
    if args.content is None and args.session_id is None:
        print('Error: provide --content and/or --session-id', file=sys.stderr)
        sys.exit(1)
    if args.content is not None and len(args.content.encode('utf-8')) > MAX_CONTENT_LENGTH:
        print(f'Error: content exceeds {MAX_CONTENT_LENGTH} bytes', file=sys.stderr)
        sys.exit(1)
    store = args.store
    note_type, year, seq = _parse_id_arg(str(args.id), store)
    if note_type == 'learning':
        print(f'Error: {args.id} is a learning — use "unlearn" to remove it.', file=sys.stderr)
        sys.exit(1)
    note = store.get_note(note_type, year, seq)
    if not note:
        print(f'Note {args.id} not found.', file=sys.stderr)
        sys.exit(1)
    if note.get('status') == 'done':
        print(f'Error: note {args.id} is already done and cannot be updated.', file=sys.stderr)
        sys.exit(1)
    kwargs = {}
    if args.content is not None:
        kwargs['content'] = args.content
        kwargs['summary'] = args.content[:120].replace('\n', ' ').strip()
    if args.session_id is not None:
        kwargs['session'] = args.session_id
    store.update_note(note_type, year, seq, **kwargs)
    print(f'Updated {args.id}.')


def cmd_archive(args):
    store = args.store
    if args.before:
        try:
            cutoff = datetime.strptime(args.before, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Error: --before must be in YYYY-MM-DD format, got {args.before!r}", file=sys.stderr)
            sys.exit(1)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_ARCHIVE_DAYS)

    all_notes = store.list_notes(status='active')
    archived_count = 0
    for n in all_notes:
        ts = _parse_timestamp(n.get('timestamp', ''))
        archivable = (n.get('status') == 'done') or (n.get('type') == 'handoff')
        if ts is not None and ts < cutoff and archivable:
            store.archive_note(n['type'], n['year'], n['seq'])
            archived_count += 1
    if not archived_count:
        print('Nothing to archive.')
        return
    print(f"Archived {archived_count} notes (before {cutoff.strftime('%Y-%m-%d')}).")


def cmd_migrate(args):
    """Migration from old format — deferred to Phase 3."""
    print('Error: migrate command is not yet available (planned for Phase 3).', file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Learnings commands
# ---------------------------------------------------------------------------

def cmd_learn(args):
    store = args.store
    content = args.content
    if len(content.encode('utf-8')) > MAX_CONTENT_LENGTH:
        print(f'Error: content exceeds {MAX_CONTENT_LENGTH} bytes', file=sys.stderr)
        sys.exit(1)
    metadata = {}
    if getattr(args, 'role', ''):
        metadata['role'] = args.role
    if getattr(args, 'mission', ''):
        metadata['mission'] = args.mission
    entry = store.add_learning(
        content=content,
        session_id=args.session_id or '',
        **metadata,
    )
    print(f"Learned {_format_id(entry)}")


def cmd_learnings(args):
    store = args.store
    learnings = store.list_learnings(search=args.search)

    if args.role:
        learnings = [l for l in learnings if l.get('role', '').lower() == args.role.lower()]
    if args.mission:
        learnings = [l for l in learnings if l.get('mission', '').lower() == args.mission.lower()]

    if not learnings:
        print('No learnings found.')
        return

    for l in learnings:
        age = format_age(l.get('timestamp', ''))
        if args.full:
            # Read full content from store
            full = store.get_learning(l['year'], l['seq'])
            content = full.get('content', '') if full else l.get('summary', '')
            print(f'{_format_id(l)}: {content}')
        else:
            short = l.get('summary', '').replace('\n', ' ')[:80]
            print(f'{_format_id(l):<12} ({age:<9}) {short}')


def cmd_handoff_sessions(args):
    store = args.store
    handoffs = store.list_notes(note_type='handoff')
    seen = set()
    for n in handoffs:
        if n.get('status') != 'done':
            sid = n.get('session', '').strip()
            if sid and sid not in seen:
                seen.add(sid)
                print(sid)
    if not seen:
        print('(none)')


def cmd_unlearn(args):
    store = args.store
    note_type, year, seq = _parse_id_arg(args.id, store)
    if note_type != 'learning':
        print(f'Error: {args.id} is a {note_type}, not a learning.', file=sys.stderr)
        sys.exit(1)
    result = store.remove_learning(year, seq)
    if result is None:
        print(f'Learning {args.id} not found.', file=sys.stderr)
        sys.exit(1)
    print(f'Removed learning {args.id}.')


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def _folder_to_note_type(folder_name: str) -> str:
    """Invert TYPE_FOLDERS to look up note type by folder name."""
    inverse = {v: k for k, v in TYPE_FOLDERS.items()}
    return inverse.get(folder_name, 'general')


def cmd_repair(args):
    store = args.store
    state_dir = store.state_dir

    all_folder_names = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]
    if not state_dir.exists() or not any((state_dir / name).exists() for name in all_folder_names):
        print('No scribe data found')
        return 0

    dry_run = bool(getattr(args, 'dry_run', False))
    rebuilt = 0
    orphans = 0
    report_lines = []

    for type_folder_name in all_folder_names:
        type_dir = state_dir / type_folder_name
        if not type_dir.exists():
            continue
        note_type = 'learning' if type_folder_name == LEARNING_FOLDER else _folder_to_note_type(type_folder_name)

        # Scan year subfolders (dirs whose name is all digits)
        for child in type_dir.iterdir():
            if not child.is_dir() or not child.name.isdigit():
                continue
            year = int(child.name)
            year_dir = child

            for is_archive in (False, True):
                folder = year_dir / ARCHIVE_DIRNAME if is_archive else year_dir
                if not folder.exists():
                    continue

                entries = ScribeStore._read_index(folder)
                index_seqs = {e.get('seq'): e for e in entries if isinstance(e.get('seq'), int)}

                md_files = list(folder.glob('*.md'))
                md_seqs = set()
                for md_path in md_files:
                    try:
                        seq = int(md_path.stem)
                    except ValueError:
                        print(f'Warning: skipping non-integer filename {md_path.name} in {folder}', file=sys.stderr)
                        continue
                    md_seqs.add(seq)

                new_entries = list(entries)

                for seq in sorted(md_seqs - set(index_seqs.keys())):
                    md_path = folder / f'{seq}.md'
                    content = md_path.read_text(encoding='utf-8')
                    st_mtime = md_path.stat().st_mtime
                    ts = datetime.fromtimestamp(st_mtime, tz=timezone.utc).isoformat()
                    summary = content[:200].replace('\n', ' ').strip()
                    prefix = TYPE_PREFIXES.get(note_type, 'G')
                    display_id = f"{prefix}-{year}-{seq}"
                    entry = {
                        'display_id': display_id,
                        'type': note_type,
                        'year': year,
                        'seq': seq,
                        'status': 'archived' if is_archive else 'active',
                        'session': '',
                        'timestamp': ts,
                        'summary': summary,
                        'has_body': bool(content),
                    }
                    new_entries.append(entry)
                    rebuilt += 1
                    report_lines.append(f'  + rebuilt entry {display_id} in {folder.relative_to(state_dir)}')

                filtered_entries = []
                for entry in new_entries:
                    eseq = entry.get('seq')
                    if isinstance(eseq, int) and eseq not in md_seqs:
                        orphans += 1
                        report_lines.append(f'  - orphan entry seq={eseq} in {folder.relative_to(state_dir)}')
                    else:
                        filtered_entries.append(entry)

                folder_changed = (len(new_entries) != len(filtered_entries)) or (len(new_entries) != len(entries))
                if not dry_run and folder_changed:
                    ScribeStore._write_index(folder, filtered_entries)

                # Rebuild next_seq for active year_dir (not archive)
                if not is_archive:
                    max_seq = max(
                        (e.get('seq', 0) for e in filtered_entries if isinstance(e.get('seq'), int)),
                        default=0,
                    )
                    arc_dir = year_dir / ARCHIVE_DIRNAME
                    if arc_dir.exists():
                        for ae in ScribeStore._read_index(arc_dir):
                            s = ae.get('seq', 0)
                            if isinstance(s, int) and s > max_seq:
                                max_seq = s
                    new_next_seq = max_seq + 1
                    seq_path = year_dir / NEXT_SEQ_FILENAME
                    current_seq = 1
                    if seq_path.exists():
                        try:
                            current_seq = int(seq_path.read_text(encoding='utf-8').strip())
                        except ValueError:
                            current_seq = 1
                    if new_next_seq != current_seq:
                        if not dry_run:
                            seq_path.write_text(str(new_next_seq), encoding='utf-8')
                        report_lines.append(
                            f'  * next_seq for {year_dir.relative_to(state_dir)}: {current_seq} -> {new_next_seq}'
                        )

    print(
        f'Repair {"(dry-run) " if dry_run else ""}complete: '
        f'{rebuilt} entries rebuilt, '
        f'{orphans} orphans {"detected" if dry_run else "removed"}'
    )
    for line in report_lines:
        print(line)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scribe — structured note management")
    parser.add_argument("--project", default=None,
                        help="Project key (e.g. D--Professional-claude-apiary). Defaults to cwd-derived key.")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("--type", required=True, choices=VALID_TYPES)
    p_add.add_argument("--content", required=True)
    p_add.add_argument("--summary", default="",
                        help="One-line abstract shown in lists and startup. Required for --type handoff.")
    p_add.add_argument("--session-id", default="")
    p_add.add_argument("--auto", action="store_true", help="Mark as auto-generated")
    p_add.add_argument("--if-no-handoff-for", default=None,
                        help="Only add if no handoff exists for this session ID")
    p_add.add_argument("--role", default="", help="Session role (e.g. user, attacker)")
    p_add.add_argument("--mission", default="", help="Session mission (e.g. general, project-x)")

    # list
    p_list = sub.add_parser("list")
    p_list.add_argument("--type", choices=VALID_TYPES)
    p_list.add_argument("--session")
    p_list.add_argument("--search")
    p_list.add_argument("--last", "--limit", type=int, dest="last",
                        help="Show only the N most recent matching notes (--limit is an alias)")
    p_list.add_argument("--all", action="store_true", help="Include done, dropped, and deferred notes")
    p_list.add_argument("--deferred", action="store_true", help="Show only deferred notes")
    p_list.add_argument("--archive", action="store_true", help="Search archive instead")
    p_list.add_argument("--role", help="Filter by session role")
    p_list.add_argument("--mission", help="Filter by session mission")

    # get / show
    p_get = sub.add_parser("get", aliases=["show"])
    p_get.add_argument("id", type=str, help="Note ID (integer) or learning ID (L-prefix, e.g. L3)")

    # done
    p_done = sub.add_parser("done")
    p_done.add_argument("id", type=str)

    # drop — close without claiming completion
    p_drop = sub.add_parser("drop")
    p_drop.add_argument("id", type=str)

    # defer — hide from default listings without closing
    p_defer = sub.add_parser("defer")
    p_defer.add_argument("id", type=str)

    # resume — undo defer; returns note to active
    p_resume = sub.add_parser("resume")
    p_resume.add_argument("id", type=str)

    # unarchive — move a note back from its year's archive to active
    p_unarchive = sub.add_parser("unarchive")
    p_unarchive.add_argument("id", type=str)

    # update
    p_update = sub.add_parser("update")
    p_update.add_argument("id", type=str)
    p_update.add_argument("--content", default=None)
    p_update.add_argument("--session-id", default=None)

    # archive
    p_archive = sub.add_parser("archive")
    p_archive.add_argument("--before", help="Archive notes before this date (YYYY-MM-DD)")

    # migrate
    p_migrate = sub.add_parser("migrate")
    p_migrate.add_argument("path", help="Path to old notes.md file")

    # learn
    p_learn = sub.add_parser("learn")
    p_learn.add_argument("--content", required=True)
    p_learn.add_argument("--session-id", default="")
    p_learn.add_argument("--role", default="", help="Session role")
    p_learn.add_argument("--mission", default="", help="Session mission")

    # learnings
    p_learnings = sub.add_parser("learnings")
    p_learnings.add_argument("--search")
    p_learnings.add_argument("--full", action="store_true", help="Print full content (not truncated)")
    p_learnings.add_argument("--role", help="Filter by session role")
    p_learnings.add_argument("--mission", help="Filter by session mission")

    # handoff-sessions
    sub.add_parser("handoff-sessions")

    # unlearn
    p_unlearn = sub.add_parser("unlearn")
    p_unlearn.add_argument("id", type=str, help="Learning ID (integer or L-prefix, e.g. L3 or 3)")

    p_repair = sub.add_parser("repair")
    p_repair.add_argument("--dry-run", action="store_true", help="Report what would be fixed without modifying files")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Auto-fill role/mission/session_id from session identity if not provided.
    # Only called after confirming a command is present so --help works without
    # a valid session identity file. Only write commands inherit the identity —
    # read commands treat an absent --role/--mission as "no filter" so legacy
    # entries without role/mission stay visible.
    default_role, default_mission, default_sid = _load_session_identity()
    _WRITE_COMMANDS = {"add", "learn", "update"}
    if args.command in _WRITE_COMMANDS:
        if hasattr(args, "role") and not getattr(args, "role", ""):
            args.role = default_role
        if hasattr(args, "mission") and not getattr(args, "mission", ""):
            args.mission = default_mission
    # Auto-fill session_id for handoffs so manual handoffs get tagged correctly
    if (hasattr(args, "session_id") and not getattr(args, "session_id", "")
            and getattr(args, "type", "") == "handoff"):
        args.session_id = default_sid

    # Initialize ScribeStore
    pk = _project_key(args.project)
    state_dir = scribe_state_dir() or (PROJECTS_DIR / pk)
    args.store = ScribeStore(state_dir)

    commands = {
        'add': cmd_add, 'list': cmd_list, 'get': cmd_get, 'show': cmd_get,
        'done': cmd_done, 'drop': cmd_drop, 'defer': cmd_defer, 'resume': cmd_resume,
        'unarchive': cmd_unarchive,
        'update': cmd_update, 'archive': cmd_archive,
        'learn': cmd_learn, 'learnings': cmd_learnings, 'unlearn': cmd_unlearn,
        'handoff-sessions': cmd_handoff_sessions,
        'migrate': cmd_migrate,
        'repair': cmd_repair,
    }
    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
