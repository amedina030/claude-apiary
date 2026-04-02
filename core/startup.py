#!/usr/bin/env python3
"""
Session startup consolidation — replaces multiple agent tool calls with two Python commands.

Usage:
    startup.py init --session-id X --first-message "..." --repo-dir /path
    startup.py summary --repo-dir /path
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.session import CLAUDE_DIR, SessionId, load_identity
from scribe.notes import (
    read_jsonl, notes_path, learnings_path,
    project_key_from_path, format_age,
)

HISTORY_PATH = CLAUDE_DIR / ".session-history.json"
REGISTRY_PATH = PROJECT_ROOT / "core" / "config" / "session-registry.json"


# ---------------------------------------------------------------------------
# init command
# ---------------------------------------------------------------------------

def parse_identity(first_message):
    """Parse structured identity from first message, or return defaults."""
    defaults = {
        "role": "user",
        "mission": "general",
        "wants_role": "user",
        "wants_mission": "general",
    }
    if not first_message:
        return defaults

    role_m = re.search(r'^role:\s*(.+)$', first_message, re.MULTILINE | re.IGNORECASE)
    mission_m = re.search(r'^mission:\s*(.+)$', first_message, re.MULTILINE | re.IGNORECASE)

    if not role_m and not mission_m:
        return defaults

    role = role_m.group(1).strip() if role_m else "user"
    mission = mission_m.group(1).strip() if mission_m else "general"

    wants_role = role
    wants_mission = mission
    wants_m = re.search(r'^wants:\s*(.+)$', first_message, re.MULTILINE | re.IGNORECASE)
    if wants_m:
        parts = wants_m.group(1).strip().split(None, 1)
        wants_role = parts[0] if parts else role
        wants_mission = parts[1] if len(parts) > 1 else mission

    return {
        "role": role,
        "mission": mission,
        "wants_role": wants_role,
        "wants_mission": wants_mission,
    }


def validate_registry(role, mission):
    """Check if role and mission are in the session registry."""
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return role in data.get("roles", []) and mission in data.get("missions", [])
    except (OSError, json.JSONDecodeError):
        return False


def get_unseen_sessions(session_id, wants_role, wants_mission, project_key):
    """Find sessions that match wants and don't have handoff notes yet."""
    # Read session history
    if not HISTORY_PATH.exists():
        return []
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(history, list):
        return []

    sid = SessionId(session_id)

    # Filter: not current session, matches wants
    matching = [
        s for s in history
        if not sid.matches(s.get("session_id", ""))
        and s.get("role", "user") == wants_role
        and s.get("mission", "general") == wants_mission
    ]

    # Get existing handoff session IDs from notes
    notes = read_jsonl(notes_path(project_key))
    handoff_sids = {
        n.get("session_id", "").strip()
        for n in notes
        if n.get("type") == "handoff" and n.get("status") != "done"
    }

    # Filter to unseen
    unseen = []
    for s in matching:
        s_short = s.get("session_id", "")[:8].lower()
        if s_short and s_short not in handoff_sids:
            unseen.append({
                "session_id": s.get("session_id", ""),
                "transcript_path": s.get("transcript_path", ""),
                "role": s.get("role", "user"),
                "mission": s.get("mission", "general"),
            })

    return unseen


def cmd_init(args):
    identity = parse_identity(args.first_message)
    registered = validate_registry(identity["role"], identity["mission"])

    # Write identity file
    sid = SessionId(args.session_id)
    identity_file = sid.identity_path()
    identity_data = {
        "role": identity["role"],
        "mission": identity["mission"],
        "registered": registered,
        "wants_role": identity["wants_role"],
        "wants_mission": identity["wants_mission"],
    }
    identity_file.write_text(json.dumps(identity_data), encoding="utf-8")

    # Find unseen sessions
    project_key = project_key_from_path(args.repo_dir)
    unseen = get_unseen_sessions(
        args.session_id,
        identity["wants_role"],
        identity["wants_mission"],
        project_key,
    )

    result = {
        "identity": {**identity_data, "registered": registered},
        "unseen_sessions": unseen,
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# summary command
# ---------------------------------------------------------------------------

def _matches_role_mission(note, role, mission):
    """Check if a note matches the given role/mission (or has no role/mission set)."""
    n_role = note.get("role", "user")
    n_mission = note.get("mission", "general")
    return n_role == role and n_mission == mission


def cmd_summary(args):
    repo_dir = args.repo_dir or str(PROJECT_ROOT)
    project_key = project_key_from_path(repo_dir)
    role = args.role or "user"
    mission = args.mission or "general"

    notes = read_jsonl(notes_path(project_key))
    learnings = read_jsonl(learnings_path(project_key))

    # Active, unresolved notes matching role/mission
    active = [
        n for n in notes
        if n.get("status") not in ("done", "resolved")
        and _matches_role_mission(n, role, mission)
    ]

    # Format active items list
    items = []
    for n in active:
        if n.get("type") == "handoff":
            continue  # Don't list handoffs in active items
        nid = n.get("id", "?")
        ntype = n.get("type", "?")
        content = n.get("content", "").replace("\n", " ")[:40]
        items.append(f"#{nid} {ntype} ({content})")

    # Format learnings list (filtered by role/mission)
    learning_items = []
    for l in learnings:
        if not _matches_role_mission(l, role, mission):
            continue
        lid = l.get("id", "?")
        content = l.get("content", "").replace("\n", " ")[:60]
        learning_items.append(f"#{lid} {content}")

    # Find latest handoff matching role/mission
    handoffs = [
        n for n in active
        if n.get("type") == "handoff"
    ]
    latest_handoff = max(handoffs, key=lambda n: n.get("id", 0)) if handoffs else None

    # Print summary
    print(f"**Active items:** {len(items)} notes — {', '.join(items) if items else 'None'}")
    print()
    print(f"**Learnings:** {len(learning_items)} — {', '.join(learning_items) if learning_items else 'None'}")
    print()

    if latest_handoff:
        hid = latest_handoff.get("id", "?")
        hsid = latest_handoff.get("session_id", "?")
        print(f"**Last session (#{hid}, {hsid}):**")
        print(latest_handoff.get("content", ""))
    else:
        print("**Last session:** No handoff notes found.")

    # Run docs conformance check
    check_script = PROJECT_ROOT / "docs" / "check.py"
    if check_script.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(check_script)],
                capture_output=True, text=True, timeout=10,
                cwd=str(PROJECT_ROOT),
            )
            if result.returncode != 0:
                print()
                print(f"**Docs:** {result.stdout.strip()}")
            else:
                # Clean — just show the count
                print()
                print(f"**Docs:** {result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError):
            pass  # Don't block startup on check failure


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Session startup consolidation")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init")
    p_init.add_argument("--session-id", required=True)
    p_init.add_argument("--first-message", required=True)
    p_init.add_argument("--repo-dir", required=True)

    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--repo-dir", default=None)
    p_summary.add_argument("--role", default="user")
    p_summary.add_argument("--mission", default="general")

    args = parser.parse_args()

    commands = {"init": cmd_init, "summary": cmd_summary}
    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
