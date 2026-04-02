#!/usr/bin/env python3
"""
PreToolUse hook — loads scribe notes on first tool call of each session.

On first call:
1. Checks if previous session is missing a handoff note
2. Injects active TODOs/blockers and last handoff into context
3. If handoff is missing, instructs Claude to generate one from .last-transcript.jsonl

Runs once per session via flag file.
"""
import sys
import json
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
NOTES_PATH = CLAUDE_DIR / "notes.jsonl"
TRANSCRIPT_PATH = CLAUDE_DIR / ".last-transcript.jsonl"
SESSION_PATH = CLAUDE_DIR / ".last-session.json"
SESSION_FLAG_DIR = CLAUDE_DIR / "tmp"

# Add scribe to path for notes.py imports
APIS_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(APIS_DIR))


def hook_allow(context=None):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(out))


def read_notes():
    if not NOTES_PATH.exists():
        return []
    entries = []
    for line in NOTES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def format_age(ts):
    from scribe.notes import _format_age
    return _format_age(ts)


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # Once per session
    SESSION_FLAG_DIR.mkdir(parents=True, exist_ok=True)
    flag_file = SESSION_FLAG_DIR / f"{session_id}_notes_loaded"
    if flag_file.exists():
        sys.exit(0)
    flag_file.write_text("1", encoding="utf-8")

    notes = read_notes()
    if not notes:
        sys.exit(0)

    contexts = []

    # Find the last handoff
    handoffs = [n for n in notes if n.get("type") == "handoff"]
    last_handoff = handoffs[-1] if handoffs else None
    last_handoff_id = last_handoff["id"] if last_handoff else 0

    # Check if any notes from a DIFFERENT session exist after the last handoff.
    # Notes from the current session don't count — we're still in it.
    notes_after_handoff = [
        n for n in notes
        if n.get("id", 0) > last_handoff_id
        and n.get("session_id", "")[:8] != session_id[:8]
    ]
    has_gap = len(notes_after_handoff) > 0

    # Check for missing handoff from previous session
    if has_gap and TRANSCRIPT_PATH.exists() and SESSION_PATH.exists():
        try:
            prev_session = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            prev_id = prev_session.get("session_id", "unknown")[:8]
        except (json.JSONDecodeError, KeyError):
            prev_id = "unknown"

        # Don't request handoff for the current session
        if prev_id == session_id[:8]:
            has_gap = False

    if has_gap and TRANSCRIPT_PATH.exists():
        contexts.append(
            f"[scribe] Previous session ({prev_id}) has no handoff note. "
            f"Before proceeding with the user's request, spawn a subagent to:\n"
            f"  1. Read {TRANSCRIPT_PATH} (stripped conversation transcript)\n"
            f"  2. Summarize the session: what was done, what's pending, where it stopped\n"
            f"  3. Write the handoff: python {APIS_DIR}/scribe/notes.py add "
            f"--type handoff --session-id {prev_id} --auto "
            f'--content "<summary>"\n'
            f"After the subagent finishes, read the notes to get context."
        )

    # Inject last handoff if it exists
    if last_handoff:
        age = format_age(last_handoff.get("timestamp", ""))
        content = last_handoff.get("content", "").replace("\n", " ")[:200]
        contexts.append(f"[scribe] Last handoff ({age}): {content}")

    # Inject active TODOs and blockers
    active = [n for n in notes if n.get("status") == "active"
              and n.get("type") in ("todo", "blocker")]
    if active:
        items = []
        for n in active:
            age = format_age(n.get("timestamp", ""))
            content = n.get("content", "").replace("\n", " ")[:60]
            items.append(f"#{n['id']} {n['type']} ({age}) {content}")
        contexts.append(f"[scribe] Active items:\n  " + "\n  ".join(items))

    if contexts:
        hook_allow("\n\n".join(contexts))
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
