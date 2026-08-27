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
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.session import SessionId, load_history, sessions_dir
from core.utils.project import get_project_key
from scribe.formatting import format_age, format_id as _format_id
from scribe.paths import PROJECTS_DIR, scribe_state_dir
from scribe.policy import run_auto_archive
from scribe.store import ScribeStore, TYPE_FOLDERS



def history_path() -> Path:
    """Session history ring buffer, written by core/hooks/save_transcript.py."""
    return sessions_dir() / "history.json"
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


def _compass_arm(session_short: str) -> str:
    """The compass A/B arm to stamp into this session's identity file.

    Imported lazily and failure-tolerantly: session start must not depend on
    compass being importable, and a broken config must not cost the user a
    session. Defaults to "on" — the pre-experiment behaviour.
    """
    try:
        from compass import ab
        return ab.arm_for_new_session(session_short)
    except Exception:
        return "on"


def run_init(session_id: str, first_message: str, repo_dir: str) -> dict:
    """Run init logic and return result dict with identity."""
    identity = parse_identity(first_message)
    registered = validate_registry(identity["role"], identity["mission"])

    sid = SessionId(session_id)
    identity_file = sid.identity_path()
    identity_data = {
        "role": identity["role"],
        "mission": identity["mission"],
        "registered": registered,
        "wants_role": identity["wants_role"],
        "wants_mission": identity["wants_mission"],
        # Which side of the compass A/B this session is on. Recorded here so
        # a later `ab_seed` change cannot rewrite what already happened; the
        # value is "on" for every session while the experiment is disabled,
        # which is the shipped default (compass/config.json).
        "compass_arm": _compass_arm(sid.short),
    }
    identity_file.parent.mkdir(parents=True, exist_ok=True)
    identity_file.write_text(json.dumps(identity_data), encoding="utf-8")

    return {"identity": {**identity_data, "registered": registered}}


def cmd_init(args):
    result = run_init(args.session_id, args.first_message, args.repo_dir)
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# summary command
# ---------------------------------------------------------------------------

def _matches_role_mission(note, role, mission):
    """Check if a note matches the given role/mission (or has no role/mission set)."""
    n_role = note.get("role", "user")
    n_mission = note.get("mission", "general")
    return n_role == role and n_mission == mission


# Threshold after which the startup banner nudges the user toward
# `/review-learnings`. 30 days lines up with the "monthly review" decision
# in C-2026-33 (T-2026-152). Keep the check filesystem-based — no extra
# state file required beyond the last_review marker itself.
_LEARNINGS_REVIEW_STALE_DAYS = 30


def _review_staleness_marker(state_dir: Path) -> str:
    """Return an inline suffix for the Learnings summary line when the review
    is overdue. Returns '' when fresh, missing, or on error (fail-open)."""
    marker_path = state_dir / "learnings" / "last_review"
    try:
        if not marker_path.exists():
            return " • never reviewed — run /review-learnings"
        mtime = datetime.fromtimestamp(marker_path.stat().st_mtime, tz=timezone.utc)
        age = datetime.now(timezone.utc) - mtime
        if age.days >= _LEARNINGS_REVIEW_STALE_DAYS:
            return f" • last review {age.days}d ago — run /review-learnings"
    except OSError:
        pass
    return ""


def run_summary(repo_dir: str, role: str = "user", mission: str = "general") -> str:
    """Run summary logic and return the text output."""
    repo_dir = repo_dir or str(PROJECT_ROOT)
    project_key = get_project_key(repo_dir)
    start = Path(repo_dir)

    # Compute state_dir from the registry-resolved path; fall back to the
    # historical PROJECTS_DIR/<project_key> path when scribe_state_dir
    # can't resolve (session not inside a git repo).
    sd = scribe_state_dir(start)
    if sd is None:
        sd = PROJECTS_DIR / project_key

    store = ScribeStore(sd)

    # Prune stale notes before loading. The rules live in scribe/policy.py —
    # the same ones `notes.py add` and `notes.py tidy` apply.
    archived_count = run_auto_archive(store)

    lines = []
    if archived_count:
        lines.append(f"[auto-archived {archived_count} notes]")


    active_entries = store.list_notes(status="active")
    filtered_active = [
        n for n in active_entries
        if n.get("status") not in ("done", "resolved", "dropped", "deferred")
        and _matches_role_mission(n, role, mission)
    ]

    # Only forward-looking / live-state types land in the banner.
    # Decisions, references, general, handoffs are loadable on demand via
    # `notes.py list --type <t>` and would otherwise accumulate forever.
    BANNER_TYPES = {"todo", "wishlist", "blocker", "context"}
    items = []
    for n in filtered_active:
        if n.get("type") not in BANNER_TYPES:
            continue
        did = _format_id(n)
        ntype = n.get("type", "?")
        age = format_age(n.get("timestamp", ""))
        summary = n.get("summary", "")[:100]
        items.append(f"#{did} {ntype} ({age}) {summary}")

    learn_entries = store.list_learnings()
    learning_count = sum(1 for l in learn_entries if _matches_role_mission(l, role, mission))
    review_marker = _review_staleness_marker(sd)

    handoffs = [n for n in filtered_active if n.get("type") == "handoff"]
    latest_handoff = None
    if handoffs:
        session_times = {}
        for s in load_history(history_path()):
            sid_short = str(s.get("session_id", ""))[:8].lower()
            if sid_short and s.get("ended_at"):
                session_times[sid_short] = s["ended_at"]

        def handoff_sort_key(n):
            sid = n.get("session", n.get("session_id", ""))[:8].lower()
            return session_times.get(sid, n.get("timestamp", ""))

        latest_handoff = max(handoffs, key=handoff_sort_key)

    if latest_handoff:
        hid = _format_id(latest_handoff)
        hsid = latest_handoff.get("session", latest_handoff.get("session_id", "?"))
        summary_line = latest_handoff.get("summary", "")
        handoff_md_path = sd / TYPE_FOLDERS["handoff"] / str(latest_handoff["year"]) / f"{latest_handoff['seq']}.md"
        handoff_lines = [
            f"**Last session (#{hid}, {hsid}):** {summary_line}",
            f"  → {handoff_md_path}",
        ]
    else:
        handoff_lines = ["**Last session:** No handoff notes found."]

    # Assemble output
    lines.append(f"**Active items:** {len(items)} notes — {', '.join(items) if items else 'None'}")
    lines.append("")
    lines.append(f"**Learnings:** {learning_count} (compact index injected below){review_marker}")
    lines.append("")
    lines.extend(handoff_lines)

    # Run docs conformance check
    check_script = PROJECT_ROOT / "docs" / "check.py"
    if check_script.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(check_script)],
                capture_output=True, text=True, timeout=10,
                cwd=str(PROJECT_ROOT),
            )
            lines.append("")
            lines.append(f"**Docs:** {result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError):
            pass

    return "\n".join(lines)


def cmd_summary(args):
    output = run_summary(args.repo_dir, args.role or "user", args.mission or "general")
    print(output)


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

    commands = {
        "init": cmd_init,
        "summary": cmd_summary,
    }
    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
