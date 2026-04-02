#!/usr/bin/env python3
"""
Scribe — structured note management for LLM session continuity.

Storage: JSONL files (.claude/notes.jsonl active, .claude/notes_archive.jsonl archived).
Notes are operational state — deferred work, handoffs, decisions — not permanent facts.

Requires PYTHONUTF8=1 environment variable on Windows (set by setup.py).

Usage:
    notes.py add --type todo --content "..." --session-id X [--auto] [--role X] [--mission X]
    notes.py list [--type X] [--session X] [--search X] [--last N] [--all] [--archive] [--role X] [--mission X]
    notes.py learn --content "..." [--session-id X] [--role X] [--mission X]
    notes.py learnings [--search X] [--role X] [--mission X]
    notes.py get <id>
    notes.py done <id>
    notes.py update <id> --content "..."
    notes.py archive [--before YYYY-MM-DD]
    notes.py migrate <notes.md path>
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path as _PathImport

# Add project root to path for core.utils import
sys.path.insert(0, str(_PathImport(__file__).resolve().parent.parent))
from core.utils.filelock import FileLock

from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

VALID_TYPES = ["todo", "handoff", "decision", "wishlist", "reference", "blocker", "context"]
AUTO_ARCHIVE_DAYS = 30


def _load_session_identity():
    """Load role/mission/session_id from the most recent session identity file."""
    from core.session import load_identity
    identity = load_identity()
    return identity["role"], identity["mission"], identity["session_id"]


def project_key_from_path(p):
    """Derive Claude's project key from an absolute path.
    E.g. D:\\Professional\\claude-apis → D--Professional-claude-apis"""
    p = Path(p).resolve()
    drive = p.drive.rstrip(":\\")  # "D"
    rest = str(p).replace(p.drive + "\\", "").replace("\\", "-").replace("/", "-")
    return f"{drive}--{rest}"


def _project_key(project_override=None):
    """Return the project key, from --project flag or cwd."""
    if project_override:
        return project_override
    return project_key_from_path(Path.cwd())


def notes_path(project_key):
    return PROJECTS_DIR / project_key / "notes.jsonl"


def archive_path(project_key):
    return PROJECTS_DIR / project_key / "notes_archive.jsonl"


def learnings_path(project_key):
    return PROJECTS_DIR / project_key / "learnings.jsonl"


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def read_jsonl(path):
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n" if entries else "",
        encoding="utf-8",
    )


def _append_jsonl(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _next_id(entries):
    if not entries:
        return 1
    return max(e.get("id", 0) for e in entries) + 1


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def format_age(ts):
    """Return human-readable relative age string from an ISO timestamp."""
    dt = _parse_timestamp(ts)
    now = datetime.now(timezone.utc)
    delta = now - dt

    minutes = int(delta.total_seconds() / 60)
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


# ---------------------------------------------------------------------------
# Auto-archive
# ---------------------------------------------------------------------------

def _auto_archive(notes, notes_path, archive_path):
    """Move done notes and old handoffs past AUTO_ARCHIVE_DAYS to archive.
    Also archives handoffs older than 7 days when there are more than 10
    for the same role/mission combination.
    Returns (remaining, archived) lists."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=AUTO_ARCHIVE_DAYS)
    handoff_cutoff = now - timedelta(days=7)
    remaining = []
    to_archive = []

    # Count handoffs per role/mission
    handoff_counts = {}
    for n in notes:
        if n.get("type") == "handoff":
            key = (n.get("role", "user"), n.get("mission", "general"))
            handoff_counts[key] = handoff_counts.get(key, 0) + 1

    for n in notes:
        ts = _parse_timestamp(n.get("timestamp", ""))
        if n.get("status") == "done" and ts < cutoff:
            to_archive.append(n)
        elif n.get("type") == "handoff" and ts < cutoff:
            to_archive.append(n)
        elif n.get("type") == "handoff" and ts < handoff_cutoff:
            key = (n.get("role", "user"), n.get("mission", "general"))
            if handoff_counts.get(key, 0) > 10:
                to_archive.append(n)
            else:
                remaining.append(n)
        else:
            remaining.append(n)

    if to_archive:
        existing_archive = read_jsonl(archive_path)
        existing_archive.extend(to_archive)
        _write_jsonl(archive_path, existing_archive)
        _write_jsonl(notes_path, remaining)

    return remaining, to_archive


# ---------------------------------------------------------------------------
# Record builders — single source of truth for JSONL schemas
# ---------------------------------------------------------------------------

def make_note(notes, *, type: str, content: str, session_id: str = "",
              auto: bool = False, role: str = "", mission: str = "") -> dict:
    """Build a validated note record.

    *notes* is the current list (used for ID generation).
    Raises ValueError for invalid type.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"Invalid note type {type!r}, must be one of {VALID_TYPES}")
    return {
        "id": _next_id(notes),
        "timestamp": _now_iso(),
        "session_id": session_id,
        "type": type,
        "content": content,
        "status": "active",
        "auto_generated": auto,
        "role": role,
        "mission": mission,
    }


def make_learning(learnings, *, content: str, session_id: str = "",
                  role: str = "", mission: str = "") -> dict:
    """Build a validated learning record.

    *learnings* is the current list (used for ID generation).
    """
    return {
        "id": _next_id(learnings),
        "timestamp": _now_iso(),
        "session_id": session_id,
        "content": content,
        "role": role,
        "mission": mission,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_add(args):
    notes = read_jsonl(args.notes_path)

    # Duplicate handoff prevention
    if getattr(args, "if_no_handoff_for", None):
        target_sid = args.if_no_handoff_for.lower()
        for n in notes:
            if (n.get("type") == "handoff" and
                    n.get("session_id", "").lower().startswith(target_sid)):
                print(f"Handoff for session {target_sid} already exists (#{n['id']}). Skipping.")
                return

    try:
        note = make_note(
            notes,
            type=args.type,
            content=args.content,
            session_id=args.session_id or "",
            auto=args.auto,
            role=getattr(args, "role", "") or "",
            mission=getattr(args, "mission", "") or "",
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    _append_jsonl(args.notes_path, note)
    print(f"Added #{note['id']} ({note['type']})")


def cmd_list(args):
    if args.archive:
        notes = read_jsonl(args.archive_path)
        source = "archive"
    else:
        notes = read_jsonl(args.notes_path)
        # Auto-archive on every list call
        notes, archived = _auto_archive(notes, args.notes_path, args.archive_path)
        if archived:
            print(f"[auto-archived {len(archived)} notes]")
        source = "active"

    # Filter by status (default: hide done unless --all)
    if not args.all and not args.archive:
        notes = [n for n in notes if n.get("status") != "done"]

    # Filter by type
    if args.type:
        notes = [n for n in notes if n.get("type") == args.type]

    # Filter by session
    if args.session:
        prefix = args.session.lower()
        notes = [n for n in notes if n.get("session_id", "").lower().startswith(prefix)]

    # Filter by role
    if args.role:
        notes = [n for n in notes if n.get("role", "").lower() == args.role.lower()]

    # Filter by mission
    if args.mission:
        notes = [n for n in notes if n.get("mission", "").lower() == args.mission.lower()]

    # Filter by search
    if args.search:
        term = args.search.lower()
        notes = [n for n in notes if term in n.get("content", "").lower()]

    # Limit
    if args.last:
        notes = notes[-args.last:]

    if not notes:
        print(f"No {source} notes found.")
        return

    for n in notes:
        nid = n.get("id", "?")
        ntype = n.get("type", "?")[:8]
        age = format_age(n.get("timestamp", ""))
        status = " [DONE]" if n.get("status") == "done" else ""
        content = n.get("content", "").replace("\n", " ")[:80]
        line = f"#{nid:<4} {ntype:<10} ({age:<9}) {content}{status}"
        print(line.encode("ascii", errors="replace").decode("ascii"))


def cmd_get(args):
    notes = read_jsonl(args.notes_path)
    # Also check archive
    note = next((n for n in notes if n.get("id") == args.id), None)
    if not note:
        archive = read_jsonl(args.archive_path)
        note = next((n for n in archive if n.get("id") == args.id), None)
        if note:
            print("[from archive]")

    if not note:
        print(f"Note #{args.id} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"ID: {note['id']}")
    print(f"Type: {note.get('type', '?')}")
    print(f"Status: {note.get('status', '?')}")
    print(f"Session: {note.get('session_id', '?')}")
    print(f"Time: {note.get('timestamp', '?')} ({format_age(note.get('timestamp', ''))})")
    if note.get("role"):
        print(f"Role: {note['role']}")
    if note.get("mission"):
        print(f"Mission: {note['mission']}")
    print(f"Auto: {note.get('auto_generated', False)}")
    print(f"---")
    print(note.get("content", ""))


def cmd_done(args):
    notes = read_jsonl(args.notes_path)
    found = False
    for n in notes:
        if n.get("id") == args.id:
            n["status"] = "done"
            found = True
            break

    if not found:
        print(f"Note #{args.id} not found.", file=sys.stderr)
        sys.exit(1)

    _write_jsonl(args.notes_path, notes)
    print(f"Marked #{args.id} as done.")


def cmd_update(args):
    if args.content is None and args.session_id is None:
        print("Error: provide --content and/or --session-id", file=sys.stderr)
        sys.exit(1)
    notes = read_jsonl(args.notes_path)
    found = False
    for n in notes:
        if n.get("id") == args.id:
            if args.content is not None:
                n["content"] = args.content
            if args.session_id is not None:
                n["session_id"] = args.session_id
            found = True
            break

    if not found:
        print(f"Note #{args.id} not found.", file=sys.stderr)
        sys.exit(1)

    _write_jsonl(args.notes_path, notes)
    print(f"Updated #{args.id}.")


def cmd_archive(args):
    notes = read_jsonl(args.notes_path)
    if args.before:
        cutoff = datetime.strptime(args.before, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_ARCHIVE_DAYS)

    remaining = []
    to_archive = []
    for n in notes:
        ts = _parse_timestamp(n.get("timestamp", ""))
        if ts < cutoff:
            to_archive.append(n)
        else:
            remaining.append(n)

    if not to_archive:
        print("Nothing to archive.")
        return

    existing_archive = read_jsonl(args.archive_path)
    existing_archive.extend(to_archive)
    _write_jsonl(args.archive_path, existing_archive)
    _write_jsonl(args.notes_path, remaining)
    print(f"Archived {len(to_archive)} notes (before {cutoff.strftime('%Y-%m-%d')}).")


def cmd_migrate(args):
    """Migrate from old notes.md format to JSONL."""
    md_path = Path(args.path)
    if not md_path.exists():
        print(f"File not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    content = md_path.read_text(encoding="utf-8")
    # Split on note boundaries: lines starting with "N. ["
    pattern = re.compile(r'^(\d+)\.\s+\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+UTC)\]\s+\[session:\s*(\S+?)\]\s*(.*)', re.MULTILINE)

    existing = read_jsonl(args.notes_path)
    next_id = _next_id(existing)

    # Find all note starts
    matches = list(pattern.finditer(content))
    migrated = 0

    for i, m in enumerate(matches):
        ts_str = m.group(2)
        session_id = m.group(3)
        # Content is from after the header to the next note start (or end of file)
        start = m.start(4)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        note_content = content[start:end].strip()

        # Detect type from content prefix
        note_type = "context"
        lower = note_content.lower()
        for t in VALID_TYPES:
            if lower.startswith(t + ":") or lower.startswith(t + " "):
                note_type = t
                break
        if lower.startswith("handoff"):
            note_type = "handoff"

        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            ts_iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            ts_iso = _now_iso()

        note = {
            "id": next_id,
            "timestamp": ts_iso,
            "session_id": session_id,
            "type": note_type,
            "content": note_content,
            "status": "active",
            "auto_generated": False,
        }
        _append_jsonl(args.notes_path, note)
        next_id += 1
        migrated += 1

    print(f"Migrated {migrated} notes from {md_path} to {args.notes_path}")


# ---------------------------------------------------------------------------
# Learnings commands
# ---------------------------------------------------------------------------

def cmd_learn(args):
    """Add a new learning."""
    learnings = read_jsonl(args.learnings_path)
    entry = make_learning(
        learnings,
        content=args.content,
        session_id=args.session_id or "",
        role=getattr(args, "role", "") or "",
        mission=getattr(args, "mission", "") or "",
    )
    _append_jsonl(args.learnings_path, entry)
    print(f"Learned #{entry['id']}")


def cmd_learnings(args):
    """List all learnings."""
    learnings = read_jsonl(args.learnings_path)

    if args.role:
        learnings = [l for l in learnings if l.get("role", "").lower() == args.role.lower()]

    if args.mission:
        learnings = [l for l in learnings if l.get("mission", "").lower() == args.mission.lower()]

    if args.search:
        term = args.search.lower()
        learnings = [l for l in learnings if term in l.get("content", "").lower()]

    if not learnings:
        print("No learnings found.")
        return

    for l in learnings:
        lid = l.get("id", "?")
        age = format_age(l.get("timestamp", ""))
        content = l.get("content", "").replace("\n", " ")[:80]
        line = f"#{lid:<4} ({age:<9}) {content}"
        print(line)


def cmd_handoff_sessions(args):
    """Print session IDs of existing handoff notes, one per line."""
    notes = read_jsonl(args.notes_path)
    seen = set()
    for n in notes:
        if n.get("type") == "handoff" and n.get("status") != "done":
            sid = n.get("session_id", "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                print(sid)
    if not seen:
        print("(none)")


def cmd_unlearn(args):
    """Remove a learning by ID."""
    learnings = read_jsonl(args.learnings_path)
    remaining = [l for l in learnings if l.get("id") != args.id]

    if len(remaining) == len(learnings):
        print(f"Learning #{args.id} not found.", file=sys.stderr)
        sys.exit(1)

    _write_jsonl(args.learnings_path, remaining)
    print(f"Removed learning #{args.id}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scribe — structured note management")
    parser.add_argument("--project", default=None,
                        help="Project key (e.g. D--Professional-claude-apis). Defaults to cwd-derived key.")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("--type", required=True, choices=VALID_TYPES)
    p_add.add_argument("--content", required=True)
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
    p_list.add_argument("--last", type=int)
    p_list.add_argument("--all", action="store_true", help="Include done notes")
    p_list.add_argument("--archive", action="store_true", help="Search archive instead")
    p_list.add_argument("--role", help="Filter by session role")
    p_list.add_argument("--mission", help="Filter by session mission")

    # get
    p_get = sub.add_parser("get")
    p_get.add_argument("id", type=int)

    # done
    p_done = sub.add_parser("done")
    p_done.add_argument("id", type=int)

    # update
    p_update = sub.add_parser("update")
    p_update.add_argument("id", type=int)
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
    p_learnings.add_argument("--role", help="Filter by session role")
    p_learnings.add_argument("--mission", help="Filter by session mission")

    # handoff-sessions
    sub.add_parser("handoff-sessions")

    # unlearn
    p_unlearn = sub.add_parser("unlearn")
    p_unlearn.add_argument("id", type=int)

    args = parser.parse_args()

    # Auto-fill role/mission/session_id from session identity if not provided
    default_role, default_mission, default_sid = _load_session_identity()
    if hasattr(args, "role") and not getattr(args, "role", ""):
        args.role = default_role
    if hasattr(args, "mission") and not getattr(args, "mission", ""):
        args.mission = default_mission
    # Auto-fill session_id for handoffs so manual handoffs get tagged correctly
    if (hasattr(args, "session_id") and not getattr(args, "session_id", "")
            and getattr(args, "type", "") == "handoff"):
        args.session_id = default_sid

    # Resolve project-scoped paths
    pk = _project_key(args.project)
    args.notes_path = notes_path(pk)
    args.archive_path = archive_path(pk)
    args.learnings_path = learnings_path(pk)

    notes_commands = {
        "add": cmd_add, "list": cmd_list, "get": cmd_get,
        "done": cmd_done, "update": cmd_update, "archive": cmd_archive,
        "migrate": cmd_migrate, "handoff-sessions": cmd_handoff_sessions,
    }
    learnings_commands = {
        "learn": cmd_learn, "learnings": cmd_learnings, "unlearn": cmd_unlearn,
    }

    if args.command in notes_commands:
        with FileLock(args.notes_path):
            notes_commands[args.command](args)
    elif args.command in learnings_commands:
        with FileLock(args.learnings_path):
            learnings_commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
