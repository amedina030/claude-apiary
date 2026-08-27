#!/usr/bin/env python3
"""
Scribe — structured note management for LLM session continuity.

Storage: JSONL files (.claude/notes.jsonl active, .claude/notes_archive.jsonl archived).
Notes are operational state — deferred work, handoffs, decisions — not permanent facts.

Requires PYTHONUTF8=1 environment variable on Windows (set by setup.py).

Usage:
    notes.py add --type todo (--content "..." | --content-file PATH) --session-id X [--auto] [--role X] [--mission X]
    notes.py list [--type X] [--session X] [--search X] [--last N | --limit N] [--all] [--archive] [--role X] [--mission X]
    notes.py learn (--content "..." | --content-file PATH) [--session-id X] [--role X] [--mission X]
    notes.py learnings [--search X] [--role X] [--mission X]
    notes.py get <id>
    notes.py done <id>
    notes.py update <id> --content "..."
    notes.py archive [--before YYYY-MM-DD]
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
from scribe.store import ScribeStore, TYPE_FOLDERS, TYPE_PREFIXES, LEARNING_FOLDER, INDEX_FILENAME, ARCHIVE_DIRNAME, NEXT_SEQ_FILENAME, BRIEF_SUMMARY_MAX, derive_brief_summary, derive_summary

from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Centralized per-target state layout (C-2026-46). Scribe reads/writes
# from <state-dir>/scribe/ where <state-dir> is the registry-allocated
# folder under <apiary>/.repos/<name>-<id>/. The launcher exports
# APIARY_TARGET_STATE_DIR after the registry resolver runs; absent that
# env var, scribe_state_dir() falls back to <git-repo-root>/.apiary/scribe/
# for unmigrated targets. The APIARY_STATE_LAYOUT=legacy escape hatch
# was removed in phase 5 of the per-repo migration — the centralized
# layout is the only one now.
APIARY_STATE_DIRNAME = ".apiary"
SCRIBE_SUBDIR = "scribe"

VALID_TYPES = ["todo", "handoff", "decision", "wishlist", "reference", "blocker", "context", "general"]

# Per-type formatting templates that gate `scribe add`. A template whose
# frontmatter declares `required:` sections rejects content that omits any of
# them; a template with no `required:` is guidance the model can read via
# `notes.py template show <type>` and is never enforced. The hash-ack flow
# (T-2026-212) was removed in the 2026-08 review (§5a-B, option C) — one
# check, one attempt, `--force` to bypass.
TEMPLATES_DIRNAME = "templates"
# Bundled default templates shipped with apiary (tracked). `apiary install`
# copies these into a target's live <state-dir>/scribe/templates/ — see
# scaffold_default_templates. Not the live template dir.
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent / "default_templates"

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


def scribe_state_dir(start: Path | None = None) -> Path | None:
    """Return the scribe state *directory* under the active layout.

    Resolution order:
      1. ``APIARY_TARGET_STATE_DIR`` env var (set by the per-repo launcher
         after the registry resolver runs) — returns ``<state_dir>/scribe/``.
      2. ``<git-repo-root>/.apiary/scribe/`` via git rev-parse on *start*
         (or cwd). Pre-migration in-repo path retained for unmigrated
         targets that haven't been re-bootstrapped yet.
      3. ``None`` when *start* is not inside a git repo — callers decide
         whether to fall back further or error.
    """
    env_dir = os.environ.get("APIARY_TARGET_STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir) / SCRIBE_SUBDIR
    root = _git_repo_root(start)
    if root is None:
        return None
    return root / APIARY_STATE_DIRNAME / SCRIBE_SUBDIR


def template_path(state_dir: Path, note_type: str) -> Path:
    """Return the absolute path to the template file for a note type.

    The file may or may not exist; this helper is purely a path resolver.
    """
    return state_dir / TEMPLATES_DIRNAME / f"{note_type}.md"


def template_text(state_dir: Path, note_type: str) -> "str | None":
    """Return template body for a note type, or None if missing/empty.

    A whitespace-only file is treated the same as a missing one — both
    skip the gate. Read errors also return None so a transient I/O
    failure cannot lock the user out of writing notes.
    """
    path = template_path(state_dir, note_type)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return None
    if not text.strip():
        return None
    return text


def template_required_sections(text: str) -> list:
    """Return the ``required:`` section names declared in a template's frontmatter.

    Empty list when there is no ``required:`` key or it isn't a list. Reuses the
    tolerant frontmatter parser, so a malformed template never raises here.
    """
    from scribe.store import _parse_learning_content  # tolerant, shared frontmatter parser
    fm, _ = _parse_learning_content(text)
    req = fm.get('required')
    if isinstance(req, list):
        return [str(s).strip() for s in req if str(s).strip()]
    return []


def _section_present(content: str, name: str) -> bool:
    """True if *content* contains section *name* as a Markdown heading (any
    level 1-6) or a bold inline label (``**Name:**`` / ``**Name**``)."""
    esc = re.escape(name)
    if re.search(rf'(?im)^\s*#{{1,6}}\s+{esc}\b', content):
        return True
    return bool(re.search(rf'(?i)\*\*\s*{esc}\s*:?\s*\*\*', content))


def missing_required_sections(content: str, required: list) -> list:
    """Return the subset of *required* sections absent from *content*."""
    return [s for s in required if not _section_present(content, s)]


def scaffold_default_templates(state_dir: Path) -> list:
    """Copy bundled per-type templates into ``<state-dir>/templates/``.

    *state_dir* is the scribe state dir (``<target-state-dir>/scribe``). One
    template per note type in ``VALID_TYPES`` is copied from
    ``scribe/default_templates/`` when the bundled file exists.

    **Never overwrites.** An existing ``templates/<type>.md`` is the user's,
    edits included, so it is left alone — which also makes this safe to call
    on every ``apiary install`` (bootstrap and self-bootstrap are idempotent).
    Returns the sorted list of note types actually written.
    """
    dst_dir = Path(state_dir) / TEMPLATES_DIRNAME
    written: list = []
    for note_type in VALID_TYPES:
        src = DEFAULT_TEMPLATES_DIR / f"{note_type}.md"
        if not src.is_file():
            continue
        dst = dst_dir / f"{note_type}.md"
        if dst.exists():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
        written.append(note_type)
    return sorted(written)


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
    if days < 30:
        return f"{days // 7}w ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


_ANSI_RESET = '\x1b[0m'
_STATUS_TAG_COLORS = {'[DONE]': '32', '[DROPPED]': '31', '[DEFERRED]': '33', '[ARCHIVED]': '35'}


def _color_enabled() -> bool:
    """True when colored output is requested: FORCE_COLOR set and NO_COLOR unset.

    Deliberately never gates on stdout.isatty(): this tool runs inside the GUI
    (a non-TTY pipe) where isatty misreports and raw ANSI escapes would render
    as literal garbage in the chat pane (spec §5.13, decision #4).
    """
    return bool(os.environ.get('FORCE_COLOR')) and not os.environ.get('NO_COLOR')


def _c(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI SGR *codes* when color is enabled, else return as-is."""
    if not codes or not _color_enabled():
        return text
    return f'\x1b[{";".join(codes)}m{text}{_ANSI_RESET}'


def _color_status_tag(label: str) -> str:
    """Colorize a ' [STATUS]' label by kind; pass through when unknown/empty."""
    key = label.strip()
    code = _STATUS_TAG_COLORS.get(key)
    return f' {_c(key, code)}' if code else label


def _run_auto_archive_store(store) -> int:
    """Run auto-archive using ScribeStore. Returns count of archived notes.

    Retention rules by type:
      handoff  - keep only the latest per role/mission, archive the rest
      context  - archive after 3 days (mid-session checkpoints decay fast)
      decision - archive after 30 days (historical record, not live state)
      done     - archive 1 day after the note was *marked* done
      todo/wishlist/blocker - keep until done

    The "done" clock reads ``status_changed_at`` (stamped by update_note on
    every status transition) and only falls back to ``timestamp`` for legacy
    rows written before that field existed. Measuring from creation instead
    archived notes the moment they were closed, which is what made them
    vanish out from under a follow-up ``update``.
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
        # Done notes age out from when they were marked done, not when they
        # were created (falling back to creation only for pre-status_changed_at
        # rows). The rest of the chain is unchanged, so a done note that is not
        # yet 1 day old can still be archived by its type's own rule.
        done_ts = _parse_timestamp(n.get('status_changed_at', '')) or ts
        if n.get('status') == 'done' and done_ts < done_cutoff:
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


def _content_from_args(args):
    """Return note content from ``--content-file`` if given, else ``--content``.

    ``--content-file`` is the list-form-subprocess rule made real: a caller with
    a multi-kilobyte body (an incubator spec, a /wrapup handoff) cannot put it on
    argv — Windows ``CreateProcess`` caps the command line at 32,767 chars — and
    quoting it through a shell mangles backticks and ``/``-prefixed tokens.
    """
    content_file = getattr(args, 'content_file', None)
    if content_file:
        try:
            return Path(content_file).read_text(encoding='utf-8')
        except OSError as e:
            print(f'Error: cannot read --content-file {content_file!r}: {e}', file=sys.stderr)
            sys.exit(1)
    return args.content


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

    content = _content_from_args(args)
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

    brief_summary = (getattr(args, 'brief_summary', '') or '').strip()
    if len(brief_summary) > BRIEF_SUMMARY_MAX:
        print(
            f'Error: --brief-summary exceeds {BRIEF_SUMMARY_MAX} chars ({len(brief_summary)}). '
            'Keep it to a one-sentence headline.',
            file=sys.stderr,
        )
        sys.exit(1)

    # Template gate (§5a-B option C): one check, one attempt. When the type's
    # template declares `required:` sections, content missing any of them is
    # rejected with the template inlined so the retry has what it needs. A
    # template with no `required:` is guidance only and never blocks. --force
    # bypasses the check and says so on stderr. Forward-only: this runs on
    # `add`, never on existing notes.
    tpl_text = template_text(store.state_dir, args.type)
    force = bool(getattr(args, 'force', False))
    required = template_required_sections(tpl_text) if tpl_text is not None else []
    if required:
        missing = missing_required_sections(content, required)
        if missing and force:
            print(
                f'[template gate bypassed via --force: {args.type} note missing '
                f'{", ".join(missing)}]',
                file=sys.stderr,
            )
        elif missing:
            print(
                f'content missing required section(s): {", ".join(missing)}\n'
                f'  template: {template_path(store.state_dir, args.type)}\n\n'
                f'--- {args.type}.md ---\n'
                f'{tpl_text.rstrip()}\n'
                f'--- end ---\n\n'
                f'Add the missing section(s) and re-run, or pass --force to bypass.',
                file=sys.stderr,
            )
            sys.exit(1)

    # Tag handling + --unique-tag dedup guard (spec §5.14). --unique-tag skips
    # the add (exit 0) when any active note already carries the tag; otherwise
    # the tag is added to this note.
    tags_csv = (getattr(args, 'tags', '') or '').strip()
    tags = [t.strip() for t in tags_csv.split(',') if t.strip()] if tags_csv else []
    unique_tag = (getattr(args, 'unique_tag', '') or '').strip()
    if unique_tag:
        existing = store.find_active_with_tag(unique_tag)
        if existing is not None:
            brief = existing.get('brief_summary') or existing.get('summary') or ''
            print(f"skipped (active {_format_id(existing)} already carries tag '{unique_tag}'): {brief}")
            return
        if unique_tag not in tags:
            tags.append(unique_tag)

    # Build metadata dict for extra fields
    metadata = {}
    if getattr(args, 'auto', False):
        metadata['auto_generated'] = True
    if getattr(args, 'role', ''):
        metadata['role'] = args.role
    if getattr(args, 'mission', ''):
        metadata['mission'] = args.mission
    if tags:
        metadata['tags'] = tags

    entry = store.add_note(
        note_type=args.type,
        content=content,
        session_id=args.session_id or '',
        summary=summary,
        brief_summary=brief_summary,
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

    # `list` is a read command and no longer mutates state. Auto-archive runs
    # from `add`, from session startup, and on demand via `notes.py tidy` —
    # listing notes must not make them disappear (Phase 1.5).

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

    color = _color_enabled()
    for n in notes_list:
        ntype = n.get('type', '?')[:8]
        age = format_age(n.get('timestamp', ''))
        st = n.get('status', '')
        status_label = f' [{st.upper()}]' if st in ('done', 'dropped', 'deferred') else ''
        # For store entries, content is in 'summary' field; read full content for display
        content = n.get('summary', '').replace('\n', ' ')[:80]
        did = _format_id(n)
        if color:
            print(f"{_c(f'{did:<12}', '1', '36')} {_c(f'{ntype:<10}', '90')} "
                  f"{_c(f'({age:<9})', '2')} {content}{_color_status_tag(status_label)}")
        else:
            line = f'{did:<12} {ntype:<10} ({age:<9}) {content}{status_label}'
            print(line.encode('ascii', errors='replace').decode('ascii'))


def _format_id(entry: dict) -> str:
    """Return the display ID string for a note/learning entry."""
    prefix = TYPE_PREFIXES.get(entry.get('type', ''), '?')
    return f"{prefix}-{entry.get('year', '?')}-{entry.get('seq', '?')}"


def _parse_id_arg(raw: str, store) -> tuple:
    """Parse a TYPE-YEAR-seq ID argument (e.g. T-2026-1, L-2026-3).

    Returns ``(note_type, year, seq)``; exits 1 on anything else.
    """
    raw = raw.strip()
    m = re.match(r'^([A-Z])-([0-9]{4})-([0-9]+)$', raw.upper())
    if m:
        prefix, year_str, seq_str = m.group(1), m.group(2), m.group(3)
        if prefix in _PREFIX_TO_TYPE:
            return (_PREFIX_TO_TYPE[prefix], int(year_str), int(seq_str))
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


def _external_ticket_links(note: dict) -> list:
    """Return the external canonical-ticket refs (``ticket:K-<id>`` tags) on a note.

    These name tickets owned by the external Asana tool, not apiary. Marking the
    linked todo done is the mirror→canonical *close signal*; apiary holds no
    local ``K`` tickets, so it takes no local action. Only ``K``-prefixed refs
    are returned — apiary NEVER cascades ``done`` into a local note (spec
    §5.14 / A2 hazard guard), and the external id is never parsed, so a missing
    or unparseable ticket can't raise.
    """
    refs = []
    for tag in (note.get('tags') or []):
        if not isinstance(tag, str) or not tag.startswith('ticket:'):
            continue
        ref = tag.split(':', 1)[1].strip()
        if ref.upper().startswith('K-'):
            refs.append(ref)
    return refs


def _require_updated(updated, display_id: str):
    """Exit 1 when ``store.update_note`` reported that nothing was written.

    ``update_note`` is archive-aware, so ``None`` here means the note really
    is gone (deleted or moved between the read and the write) — printing
    "Marked X as done." in that case is the failure mode fixed in Phase 1.5.
    """
    if updated is None:
        print(
            f'Error: note {display_id} could not be updated — no matching index '
            'entry in the active or archive index. Run `notes.py repair`.',
            file=sys.stderr,
        )
        sys.exit(1)
    return updated


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
    _require_updated(store.update_note(note_type, year, seq, status='done'), args.id)
    # External-ticket close signal (spec §5.14 / A2). Non-destructive: surfaces
    # the link for the Asana tool to observe; never closes a local note.
    if note_type == 'todo':
        links = _external_ticket_links(note)
        if links:
            print(f'[external ticket link(s) {", ".join(links)}: close observed by the Asana tool]',
                  file=sys.stderr)
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
    _require_updated(store.update_note(note_type, year, seq, status='dropped'), args.id)
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
    _require_updated(store.update_note(note_type, year, seq, status='deferred'), args.id)
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
    _require_updated(store.update_note(note_type, year, seq, status='active'), args.id)
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
    brief_override = (getattr(args, 'brief_summary', '') or '').strip()
    add_tags = [t for t in (getattr(args, 'add_tag', None) or []) if t]
    remove_tags = [t for t in (getattr(args, 'remove_tag', None) or []) if t]
    if (args.content is None and args.session_id is None and not brief_override
            and not add_tags and not remove_tags):
        print('Error: provide --content, --session-id, --brief-summary, --add-tag, and/or --remove-tag',
              file=sys.stderr)
        sys.exit(1)
    if args.content is not None and len(args.content.encode('utf-8')) > MAX_CONTENT_LENGTH:
        print(f'Error: content exceeds {MAX_CONTENT_LENGTH} bytes', file=sys.stderr)
        sys.exit(1)
    if len(brief_override) > BRIEF_SUMMARY_MAX:
        print(
            f'Error: --brief-summary exceeds {BRIEF_SUMMARY_MAX} chars ({len(brief_override)}).',
            file=sys.stderr,
        )
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
    # A done note's content is frozen, but tag/brief/session mutation is still
    # allowed — the Asana tool flips status tags on closed mirrors (spec §5.14).
    if note.get('status') == 'done' and args.content is not None:
        print(f'Error: note {args.id} is already done; cannot edit content.', file=sys.stderr)
        sys.exit(1)
    kwargs = {}
    if args.content is not None:
        kwargs['content'] = args.content
        kwargs['summary'] = derive_summary(args.content)
    if args.session_id is not None:
        kwargs['session'] = args.session_id
    if brief_override:
        kwargs['brief_summary'] = brief_override
    if add_tags:
        kwargs['add_tags'] = add_tags
    if remove_tags:
        kwargs['remove_tags'] = remove_tags
    _require_updated(store.update_note(note_type, year, seq, **kwargs), args.id)
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


def cmd_tidy(args):
    """Run the auto-archive sweep explicitly.

    The same policy `add` and session startup apply, exposed as a verb so a
    read command (`list`) never has to mutate state to keep the active view
    tidy. Safe to run any time; prints what it moved.
    """
    archived = _run_auto_archive_store(args.store)
    if not archived:
        print('Nothing to tidy.')
        return
    print(f'Tidied {archived} notes into the archive.')


def cmd_mark_reviewed(args):
    """Stamp the learnings review marker the startup banner reads.

    Writes/touches ``<state-dir>/scribe/learnings/last_review``; its mtime is
    what ``core/startup._review_staleness_marker`` compares against the
    30-day threshold. Exists so `/review-learnings` can call a verb through
    the launcher instead of a `python -c` one-liner that resolved the wrong
    state dir.
    """
    marker = args.store.state_dir / LEARNING_FOLDER / 'last_review'
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('', encoding='utf-8')
        os.utime(marker)
    except OSError as e:
        print(f'Error: could not stamp {marker}: {e}', file=sys.stderr)
        sys.exit(1)
    print(f'Stamped learnings review: {marker}')


# ---------------------------------------------------------------------------
# Learnings commands
# ---------------------------------------------------------------------------

def cmd_learn(args):
    store = args.store
    content = _content_from_args(args)
    if len(content.encode('utf-8')) > MAX_CONTENT_LENGTH:
        print(f'Error: content exceeds {MAX_CONTENT_LENGTH} bytes', file=sys.stderr)
        sys.exit(1)
    brief_summary = (getattr(args, 'brief_summary', '') or '').strip()
    if len(brief_summary) > BRIEF_SUMMARY_MAX:
        print(
            f'Error: --brief-summary exceeds {BRIEF_SUMMARY_MAX} chars ({len(brief_summary)}).',
            file=sys.stderr,
        )
        sys.exit(1)
    metadata = {}
    if getattr(args, 'role', ''):
        metadata['role'] = args.role
    if getattr(args, 'mission', ''):
        metadata['mission'] = args.mission

    tags_csv = (getattr(args, 'tags', '') or '').strip()
    tags = [t.strip() for t in tags_csv.split(',') if t.strip()] if tags_csv else []
    areas = [a for a in (getattr(args, 'area', None) or []) if a]
    supersedes = (getattr(args, 'supersedes', '') or '').strip() or None

    if not tags and not areas:
        inferred = _infer_learning_tags_areas(content, store)
        tags = inferred.get('tags') or []
        areas = inferred.get('areas') or []

    entry = store.add_learning(
        content=content,
        session_id=args.session_id or '',
        brief_summary=brief_summary,
        tags=tags,
        areas=areas,
        supersedes=supersedes,
        **metadata,
    )
    print(f"Learned {_format_id(entry)}")


def _infer_learning_tags_areas(content: str, store) -> dict:
    """Call `claude -p` to infer tags and areas for a new learning.

    Fail-open: any subprocess error, timeout, or parse failure returns
    ``{}`` so the learn command still succeeds with empty lists rather
    than blocking a user mid-task. Inherits runner's subprocess hygiene
    (no shell, restricted env, stdin prompt).
    """
    try:
        sys.path.insert(0, str(_PathImport(__file__).resolve().parent.parent))
        from runner.claude_subprocess import run_claude  # lazy — avoids hot-path import cost
    except Exception:
        return {}

    try:
        vocab = sorted({
            t for l in store.list_learnings()
            for t in (l.get('tags') or [])
        })
    except Exception:
        vocab = []

    prompt = _build_inference_prompt(content, vocab)
    rc, stdout, stderr = run_claude(prompt, timeout=10)
    if rc != 0 or not stdout.strip():
        if stderr:
            print(f'warning: tag/area inference failed ({stderr[:200]})', file=sys.stderr)
        else:
            print('warning: tag/area inference failed (empty output)', file=sys.stderr)
        return {}
    parsed = _parse_inference_response(stdout)
    if parsed is None:
        print('warning: tag/area inference returned unparseable JSON', file=sys.stderr)
        return {}
    return {
        'tags': [str(t).strip() for t in parsed.get('tags', []) if str(t).strip()],
        'areas': [str(a).strip() for a in parsed.get('areas', []) if str(a).strip()],
    }


def _build_inference_prompt(content: str, vocab: list[str]) -> str:
    """Return the single-turn prompt sent to `claude -p` for tag/area inference."""
    vocab_line = ', '.join(vocab) if vocab else '(none yet)'
    return (
        "You are tagging a project learning so it can be auto-surfaced when I later\n"
        "edit related files. Respond with a JSON object only — no prose, no markdown fence.\n\n"
        f"Existing tag vocabulary: {vocab_line}\n\n"
        'Return {"tags": [...], "areas": [...]} where:\n'
        '- tags: 1-3 short lowercase tokens (prefer existing vocabulary; invent only if needed).\n'
        '- areas: glob patterns matching file paths the learning applies to (e.g. "gui/**",\n'
        '  "scribe/notes.py", "core/hooks/*.py"). Empty list if not path-specific.\n\n'
        f"Learning content:\n{content}"
    )


def _parse_inference_response(stdout: str) -> dict | None:
    """Extract the inner JSON payload from a `claude -p --output-format json`
    envelope. Returns None when parsing fails so callers can fall back."""
    try:
        envelope = json.loads(stdout)
        inner = envelope.get('result', stdout) if isinstance(envelope, dict) else stdout
    except json.JSONDecodeError:
        inner = stdout
    if not isinstance(inner, str):
        return None
    text = inner.strip()
    fence = re.search(r'```(?:json)?\s*\n([\s\S]*?)```', text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def cmd_learnings(args):
    store = args.store
    learnings = store.list_learnings(
        search=args.search,
        tag=getattr(args, 'tag', None),
        area=getattr(args, 'area', None),
    )

    if args.role:
        learnings = [l for l in learnings if l.get('role', '').lower() == args.role.lower()]
    if args.mission:
        learnings = [l for l in learnings if l.get('mission', '').lower() == args.mission.lower()]

    if not learnings:
        print('No learnings found.')
        return

    if getattr(args, 'index', False):
        _print_learnings_index(learnings)
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


def _print_learnings_index(learnings: list[dict]) -> None:
    """Render the tag-grouped compact index consumed by the startup hook.

    Primary tag = first entry in ``tags``. Missing/empty → 'untagged' bucket.
    Each group header is followed by indented one-line summaries so Claude
    can scan the map at startup and decide which areas warrant a deeper
    ``notes.py get L-X`` lookup.
    """
    groups: dict[str, list[dict]] = {}
    for l in learnings:
        tags = l.get('tags') or []
        primary = tags[0] if tags else 'untagged'
        groups.setdefault(primary, []).append(l)

    # Deterministic order: named tags alphabetically, untagged last.
    named = sorted(k for k in groups if k != 'untagged')
    order = named + (['untagged'] if 'untagged' in groups else [])

    for tag in order:
        items = groups[tag]
        print(f'[{tag}] ({len(items)})')
        for l in items:
            short = l.get('summary', '').replace('\n', ' ')[:80]
            print(f'  {_format_id(l):<12} {short}')


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


def cmd_archive_learning(args):
    """Archive a learning by ID. Parallels note archival but for the L- prefix.

    Exists so `/review-learnings` and hand-run cleanup can retire a stale
    learning without writing a replacement (which is what `supersede` is for).
    """
    store = args.store
    note_type, year, seq = _parse_id_arg(args.id, store)
    if note_type != 'learning':
        print(f'Error: {args.id} is a {note_type}, not a learning.', file=sys.stderr)
        sys.exit(1)
    result = store.archive_learning(year, seq)
    if result is None:
        print(f'Learning {args.id} not found or already archived.', file=sys.stderr)
        sys.exit(1)
    print(f'Archived learning {args.id}.')


def cmd_supersede(args):
    """Archive an old learning and write a replacement that points back at it.

    Frontmatter on the new learning carries ``supersedes: L-old`` so the
    lineage is visible when reading the .md. If ``--tags``/``--area`` are
    omitted the learn flow infers them via `claude -p`, matching the behavior
    of a fresh `notes.py learn` invocation.
    """
    store = args.store
    note_type, year, seq = _parse_id_arg(args.id, store)
    if note_type != 'learning':
        print(f'Error: {args.id} is a {note_type}, not a learning.', file=sys.stderr)
        sys.exit(1)

    existing = store.get_learning(year, seq)
    if existing is None:
        print(f'Learning {args.id} not found.', file=sys.stderr)
        sys.exit(1)
    if existing.get('_from_archive'):
        print(f'Error: {args.id} is already archived — cannot supersede.', file=sys.stderr)
        sys.exit(1)

    content = args.content
    if len(content.encode('utf-8')) > MAX_CONTENT_LENGTH:
        print(f'Error: content exceeds {MAX_CONTENT_LENGTH} bytes', file=sys.stderr)
        sys.exit(1)

    tags_csv = (getattr(args, 'tags', '') or '').strip()
    tags = [t.strip() for t in tags_csv.split(',') if t.strip()] if tags_csv else []
    areas = [a for a in (getattr(args, 'area', None) or []) if a]
    if not tags and not areas:
        inferred = _infer_learning_tags_areas(content, store)
        tags = inferred.get('tags') or []
        areas = inferred.get('areas') or []

    old_display_id = _format_id(existing)
    archived = store.archive_learning(year, seq)
    if archived is None:
        print(f'Error: failed to archive {args.id}.', file=sys.stderr)
        sys.exit(1)

    metadata = {}
    if getattr(args, 'role', ''):
        metadata['role'] = args.role
    if getattr(args, 'mission', ''):
        metadata['mission'] = args.mission
    new_entry = store.add_learning(
        content=content,
        session_id=args.session_id or '',
        tags=tags,
        areas=areas,
        supersedes=old_display_id,
        **metadata,
    )
    print(f'Superseded {old_display_id} with {_format_id(new_entry)}')


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def _folder_to_note_type(folder_name: str) -> str:
    """Invert TYPE_FOLDERS to look up note type by folder name."""
    inverse = {v: k for k, v in TYPE_FOLDERS.items()}
    return inverse.get(folder_name, 'general')


def cmd_backfill_brief(args):
    """Populate brief_summary on every existing entry that lacks one.

    Walks active + archive indices under the scribe state dir, reads each
    note's .md body, and derives a brief_summary via derive_brief_summary().
    Dry-run prints what would change without writing. One-shot migration —
    safe to re-run (it only touches entries with missing/empty brief, unless
    --force is passed which re-derives every entry).
    """
    store = args.store
    state_dir = store.state_dir
    dry_run = bool(getattr(args, 'dry_run', False))
    force = bool(getattr(args, 'force', False))

    all_folder_names = list(TYPE_FOLDERS.values()) + [LEARNING_FOLDER]
    updated = 0
    already_set = 0
    report: list[str] = []

    for folder_name in all_folder_names:
        type_dir = state_dir / folder_name
        if not type_dir.exists():
            continue
        for child in type_dir.iterdir():
            if not child.is_dir() or not child.name.isdigit():
                continue
            year_dir = child
            for is_archive in (False, True):
                folder = year_dir / ARCHIVE_DIRNAME if is_archive else year_dir
                if not folder.exists():
                    continue
                entries = ScribeStore._read_index(folder)
                changed = False
                for entry in entries:
                    seq = entry.get('seq')
                    if not isinstance(seq, int):
                        continue
                    existing = (entry.get('brief_summary') or '').strip()
                    if existing and not force:
                        already_set += 1
                        continue
                    md_path = folder / f'{seq}.md'
                    if not md_path.exists():
                        # No body — derive from the stored summary as a best effort.
                        derived = derive_brief_summary(entry.get('summary', ''))
                    else:
                        derived = derive_brief_summary(
                            md_path.read_text(encoding='utf-8', errors='replace')
                        )
                    if derived == existing:
                        already_set += 1
                        continue
                    entry['brief_summary'] = derived
                    updated += 1
                    changed = True
                    report.append(f'  + {entry.get("display_id", "?")}: {derived[:60]!r}')
                if changed and not dry_run:
                    ScribeStore._write_index(folder, entries)

    prefix = '(dry-run) ' if dry_run else ''
    print(f'Backfill {prefix}complete: {updated} updated, {already_set} already set.')
    for line in report[:50]:
        print(line)
    if len(report) > 50:
        print(f'  ... and {len(report) - 50} more')


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
                    summary = derive_summary(content)
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
                        'brief_summary': derive_brief_summary(content),
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


def cmd_template(args):
    store = args.store
    action = getattr(args, 'template_action', None)
    if action is None:
        print("Error: subcommand required (show | path | list)", file=sys.stderr)
        sys.exit(1)

    if action == 'list':
        for t in VALID_TYPES:
            text = template_text(store.state_dir, t)
            if text is None:
                continue
            required = template_required_sections(text)
            label = f"required: {', '.join(required)}" if required else 'guidance only'
            print(f"{t:10s} {label:60s} {template_path(store.state_dir, t)}")
        return

    note_type = args.type
    if action == 'path':
        print(template_path(store.state_dir, note_type))
        return

    if action == 'show':
        text = template_text(store.state_dir, note_type)
        path = template_path(store.state_dir, note_type)
        if text is None:
            print(
                f"(no template for type {note_type!r}; "
                f"create {path} to add one)",
                file=sys.stderr,
            )
            sys.exit(1)
        required = template_required_sections(text)
        label = f"required: {', '.join(required)}" if required else 'guidance only'
        print(f"# template for {note_type} ({label})")
        print(text, end='' if text.endswith('\n') else '\n')
        return


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
    p_add_content = p_add.add_mutually_exclusive_group(required=True)
    p_add_content.add_argument("--content")
    p_add_content.add_argument("--content-file", dest="content_file",
                        help="Read content from a UTF-8 file instead of --content. Use this when "
                             "content contains backticks, /-prefixed tokens, or filenames that would "
                             "otherwise trigger shell command substitution in the caller.")
    p_add.add_argument("--summary", default="",
                        help="One-line abstract shown in lists and startup. Required for --type handoff.")
    p_add.add_argument("--brief-summary", default="", dest="brief_summary",
                        help=f"One-sentence headline (<={BRIEF_SUMMARY_MAX} chars) for GUI sidebar. Auto-derived from content if omitted.")
    p_add.add_argument("--session-id", default="")
    p_add.add_argument("--auto", action="store_true", help="Mark as auto-generated")
    p_add.add_argument("--if-no-handoff-for", default=None,
                        help="Only add if no handoff exists for this session ID")
    p_add.add_argument("--role", default="", help="Session role (e.g. user, attacker)")
    p_add.add_argument("--mission", default="", help="Session mission (e.g. general, project-x)")
    p_add.add_argument("--tags", default="",
                        help="Comma-separated tags stored on the note (e.g. 'ticket:K-2026-99,priority:high').")
    p_add.add_argument("--unique-tag", default="", dest="unique_tag",
                        help="Add this tag only if no active note already carries it; "
                             "otherwise skip the add and exit 0.")
    p_add.add_argument("--force", action="store_true",
                        help="Bypass the template gate's required-section check "
                             "(logs a line to stderr naming what was missing).")

    # template — inspect per-type formatting templates that gate `add`
    p_template = sub.add_parser("template",
                                help="Inspect per-type formatting templates that gate `add`")
    p_template_sub = p_template.add_subparsers(dest="template_action")
    p_template_show = p_template_sub.add_parser("show",
                                                help="Print the template content for a type")
    p_template_show.add_argument("type", choices=VALID_TYPES)
    p_template_path = p_template_sub.add_parser("path",
                                                help="Print the absolute file path for a type's template")
    p_template_path.add_argument("type", choices=VALID_TYPES)
    p_template_sub.add_parser("list",
                              help="List types whose template file exists and is non-empty")

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
    p_get.add_argument("id", type=str, help="Note or learning ID (e.g. T-2026-1, L-2026-3)")

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
    p_update.add_argument("--brief-summary", default="", dest="brief_summary",
                          help=f"Replace brief_summary (<={BRIEF_SUMMARY_MAX} chars).")
    p_update.add_argument("--add-tag", action="append", default=[], dest="add_tag",
                          help="Add a tag (repeatable). Idempotent; order-preserving.")
    p_update.add_argument("--remove-tag", action="append", default=[], dest="remove_tag",
                          help="Remove a tag (repeatable). Applied before --add-tag.")

    # archive
    p_archive = sub.add_parser("archive")
    p_archive.add_argument("--before", help="Archive notes before this date (YYYY-MM-DD)")

    # tidy — run the auto-archive sweep on demand (list no longer does it)
    sub.add_parser("tidy",
                   help="Run the auto-archive retention sweep now (add and startup run it too)")

    # mark-reviewed — stamp learnings/last_review for the startup nudge
    sub.add_parser("mark-reviewed",
                   help="Stamp the learnings review timestamp the startup banner reads")

    # learn
    p_learn = sub.add_parser("learn")
    p_learn_content = p_learn.add_mutually_exclusive_group(required=True)
    p_learn_content.add_argument("--content")
    p_learn_content.add_argument("--content-file", dest="content_file",
                        help="Read content from a UTF-8 file instead of --content. Use this when "
                             "content contains backticks, /-prefixed tokens, or filenames that would "
                             "otherwise trigger shell command substitution in the caller.")
    p_learn.add_argument("--session-id", default="")
    p_learn.add_argument("--brief-summary", default="", dest="brief_summary",
                          help=f"One-sentence headline (<={BRIEF_SUMMARY_MAX} chars) for GUI sidebar. Auto-derived if omitted.")
    p_learn.add_argument("--role", default="", help="Session role")
    p_learn.add_argument("--mission", default="", help="Session mission")
    p_learn.add_argument("--tags", default="",
                          help="Comma-separated tag list. Omit to infer via claude -p.")
    p_learn.add_argument("--area", action="append", default=[],
                          help="Area glob pattern (repeatable). Omit to infer via claude -p.")
    p_learn.add_argument("--supersedes", default="",
                          help="ID of a prior learning this one replaces (e.g. L-2026-5).")

    # learnings
    p_learnings = sub.add_parser("learnings")
    p_learnings.add_argument("--search")
    p_learnings.add_argument("--full", action="store_true", help="Print full content (not truncated)")
    p_learnings.add_argument("--role", help="Filter by session role")
    p_learnings.add_argument("--mission", help="Filter by session mission")
    p_learnings.add_argument("--tag", help="Filter by tag (substring match, case-insensitive)")
    p_learnings.add_argument("--area", help="Filter by area glob (exact match)")
    p_learnings.add_argument("--index", action="store_true",
                             help="Compact tag-grouped output for startup injection")

    # unlearn
    p_unlearn = sub.add_parser("unlearn")
    p_unlearn.add_argument("id", type=str, help="Learning ID (e.g. L-2026-3)")

    # archive-learning — move a learning to archive (parallels note archival)
    p_archive_l = sub.add_parser("archive-learning",
                                  help="Archive a learning by ID (move to learnings/<year>/archive/)")
    p_archive_l.add_argument("id", type=str, help="Learning ID (e.g. L-2026-5)")

    # supersede — archive old learning + write replacement
    p_supersede = sub.add_parser("supersede",
                                  help="Archive an existing learning and write a replacement")
    p_supersede.add_argument("id", type=str, help="ID of the learning to supersede (e.g. L-2026-5)")
    p_supersede.add_argument("--content", required=True, help="Content of the replacement learning")
    p_supersede.add_argument("--session-id", default="")
    p_supersede.add_argument("--tags", default="",
                              help="Comma-separated tag list. Omit to infer via claude -p.")
    p_supersede.add_argument("--area", action="append", default=[],
                              help="Area glob pattern (repeatable). Omit to infer via claude -p.")
    p_supersede.add_argument("--role", default="")
    p_supersede.add_argument("--mission", default="")

    p_repair = sub.add_parser("repair")
    p_repair.add_argument("--dry-run", action="store_true", help="Report what would be fixed without modifying files")

    # backfill-brief — one-shot migration: populate brief_summary on existing notes.
    p_bf = sub.add_parser("backfill-brief",
                          help="Populate brief_summary on entries that don't have one yet")
    p_bf.add_argument("--dry-run", action="store_true",
                       help="Report what would change without writing")
    p_bf.add_argument("--force", action="store_true",
                       help="Re-derive brief_summary even for entries that already have one")

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
        'update': cmd_update, 'archive': cmd_archive, 'tidy': cmd_tidy,
        'mark-reviewed': cmd_mark_reviewed,
        'learn': cmd_learn, 'learnings': cmd_learnings, 'unlearn': cmd_unlearn,
        'archive-learning': cmd_archive_learning,
        'supersede': cmd_supersede,
        'repair': cmd_repair,
        'backfill-brief': cmd_backfill_brief,
        'template': cmd_template,
    }
    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
