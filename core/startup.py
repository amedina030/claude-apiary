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

from core.session import CLAUDE_DIR, SessionId, load_identity
from core.utils.project import get_project_key
from scribe.notes import (
    format_age, run_auto_archive, scribe_state_dir, _use_repo_layout,
    PROJECTS_DIR, _format_id,
)
from scribe.store import ScribeStore, TYPE_FOLDERS

HISTORY_PATH = CLAUDE_DIR / ".session-history.json"
REGISTRY_PATH = PROJECT_ROOT / "core" / "config" / "session-registry.json"
# Legacy backfill_skip.json location. Repo layout (APIARY_STATE_LAYOUT=repo,
# decision #269 / todo #265) resolves it under the session's repo root
# instead via _resolve_backfill_skip_path(). Existing tests that monkey-patch
# this module global continue to exercise the legacy code path.
BACKFILL_SKIP_PATH = CLAUDE_DIR / "projects" / "claude-apiary" / "backfill_skip.json"


def _resolve_backfill_skip_path(start=None):
    """Return the backfill_skip.json path under the active state layout.

    Repo layout: ``<scribe_state_dir(start)>/backfill_skip.json`` when the
    session's cwd (*start*) is inside a git repo; ``None`` when it is not
    — callers must treat None as "no skip list" (empty set).

    Legacy layout: returns the module-level ``BACKFILL_SKIP_PATH`` so that
    existing monkey-patches on that global keep working verbatim.
    """
    if _use_repo_layout():
        state_dir = scribe_state_dir(start)
        if state_dir is None:
            return None
        return state_dir / "backfill_skip.json"
    return BACKFILL_SKIP_PATH


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


def load_skip_prefixes(start=None):
    """Load 8-char lowercase session-id prefixes from backfill_skip.json.

    *start* optionally points resolution at a specific session's repo
    (repo layout, todo #265). When omitted, the active layout decides
    whether to read from the legacy ~/.claude/projects/ path or the
    in-repo .apiary/scribe/ path via _resolve_backfill_skip_path().

    Tolerates missing/empty/malformed file by returning an empty set.
    """
    path = _resolve_backfill_skip_path(start)
    if path is None:
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    skipped = data.get("skipped")
    if not isinstance(skipped, list):
        return set()
    prefixes = set()
    for entry in skipped:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("session_id", "")
        if not isinstance(sid, str) or not sid:
            continue
        prefix = sid.strip()[:8].lower()
        if prefix:
            prefixes.add(prefix)
    return prefixes


def get_unseen_sessions(session_id, wants_role, wants_mission, project_key, *, start=None):
    """Find sessions that match wants and don't have handoff notes yet.

    *start* optionally points path resolution at the session's repo (used by
    the repo-layout code path in startup_prompt_hook — decision #269, todo #264).
    """
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

    # Get existing handoff session IDs from the v2 scribe store (active AND
    # archived). The auto-archive rule "keep only the latest handoff per
    # role/mission" means prior handoffs land in the archive almost
    # immediately — without scanning archived, they'd reappear as "unseen".
    # Also union in legacy flat-jsonl reads so pre-v2 layouts still resolve.
    state_dir = scribe_state_dir(start) if _use_repo_layout() else None
    if state_dir is None:
        state_dir = PROJECTS_DIR / project_key
    handoff_sids: set[str] = set()
    try:
        store = ScribeStore(state_dir)
        for h in store.list_notes(note_type="handoff", status="all"):
            sid_val = (h.get("session") or h.get("session_id") or "").strip()
            if sid_val:
                handoff_sids.add(sid_val[:8].lower())
    except (OSError, KeyError):
        pass
    handoff_sids |= load_skip_prefixes(start=start)

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


def run_init(session_id: str, first_message: str, repo_dir: str) -> dict:
    """Run init logic and return result dict (identity + unseen_sessions)."""
    identity = parse_identity(first_message)
    registered = validate_registry(identity["role"], identity["mission"])

    # Write identity file
    sid = SessionId(session_id)
    identity_file = sid.identity_path()
    identity_data = {
        "role": identity["role"],
        "mission": identity["mission"],
        "registered": registered,
        "wants_role": identity["wants_role"],
        "wants_mission": identity["wants_mission"],
    }
    identity_file.write_text(json.dumps(identity_data), encoding="utf-8")

    # Find unseen sessions. In repo layout (APIARY_STATE_LAYOUT=repo) path
    # helpers resolve state under <git-root(repo_dir)>/.apiary/scribe/; the
    # legacy layout keeps using the project key. Passing start=repo_dir makes
    # the resolution work even when the hook process's cwd differs from the
    # session's cwd.
    project_key = get_project_key(repo_dir)
    unseen = get_unseen_sessions(
        session_id,
        identity["wants_role"],
        identity["wants_mission"],
        project_key,
        start=Path(repo_dir) if repo_dir else None,
    )

    return {
        "identity": {**identity_data, "registered": registered},
        "unseen_sessions": unseen,
    }


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

    # Compute state_dir: repo layout uses git-root-relative path; legacy uses PROJECTS_DIR.
    sd = scribe_state_dir(start) if _use_repo_layout() else None
    if sd is None:
        sd = PROJECTS_DIR / project_key

    # Prune stale notes before loading
    archived_count = run_auto_archive(project_key, start=start)

    lines = []
    if archived_count:
        lines.append(f"[auto-archived {archived_count} notes]")

    store = ScribeStore(sd)

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
        if HISTORY_PATH.exists():
            try:
                history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                for s in history:
                    sid_short = s.get("session_id", "")[:8].lower()
                    if sid_short and s.get("ended_at"):
                        session_times[sid_short] = s["ended_at"]
            except (OSError, json.JSONDecodeError):
                pass

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
# skip command — append a session to backfill_skip.json
# ---------------------------------------------------------------------------

def run_skip(session_id: str, reason: str, repo_dir: str | None = None,
             date: str | None = None) -> tuple[str, Path]:
    """Append *session_id* to backfill_skip.json. Idempotent on 8-char prefix.

    Returns ("added"|"exists", path). Raises FileNotFoundError when the path
    cannot be resolved (repo layout with no git repo at *repo_dir*).
    """
    start = Path(repo_dir) if repo_dir else None
    path = _resolve_backfill_skip_path(start)
    if path is None:
        raise FileNotFoundError(
            "cannot resolve backfill_skip.json path (not inside a git repo)"
        )

    sid = session_id.strip()
    if not sid:
        raise ValueError("session_id required")

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"skipped": []}
    else:
        data = {"skipped": []}
    if not isinstance(data, dict) or not isinstance(data.get("skipped"), list):
        data = {"skipped": []}

    prefix = sid[:8].lower()
    for entry in data["skipped"]:
        if isinstance(entry, dict) and entry.get("session_id", "")[:8].lower() == prefix:
            return "exists", path

    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data["skipped"].append({
        "session_id": sid,
        "reason": reason or "unspecified",
        "date": date_str,
    })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "added", path


def cmd_skip(args):
    try:
        status, path = run_skip(args.session_id, args.reason, args.repo_dir, args.date)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    prefix = args.session_id.strip()[:8].lower()
    print(f"{status}: {prefix} ({path})")


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

    p_skip = sub.add_parser("skip", help="append a session to backfill_skip.json")
    p_skip.add_argument("--session-id", required=True)
    p_skip.add_argument("--reason", default="unspecified")
    p_skip.add_argument("--repo-dir", default=None,
                        help="session's repo root (default: cwd)")
    p_skip.add_argument("--date", default=None,
                        help="YYYY-MM-DD (default: today UTC)")

    args = parser.parse_args()

    commands = {"init": cmd_init, "summary": cmd_summary, "skip": cmd_skip}
    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
