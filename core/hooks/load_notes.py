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
TRANSCRIPT_PATH = CLAUDE_DIR / ".last-transcript.jsonl"
SESSION_PATH = CLAUDE_DIR / ".last-session.json"
SESSION_FLAG_DIR = CLAUDE_DIR / "tmp"

# Add scribe to path for notes.py imports
APIS_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(APIS_DIR))

from scribe.notes import _project_key_from_path, _notes_path


def hook_allow(context=None):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(out))


def read_notes(notes_path):
    if not notes_path.exists():
        return []
    entries = []
    for line in notes_path.read_text(encoding="utf-8").splitlines():
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

    # Resolve project-scoped notes path from session cwd
    cwd = payload.get("cwd", str(Path.cwd()))
    pk = _project_key_from_path(cwd)
    notes_path = _notes_path(pk)

    notes = read_notes(notes_path)
    if not notes:
        sys.exit(0)

    contexts = []

    # Always read the previous session transcript to generate/refresh handoff
    last_note = notes[-1] if notes else None
    last_is_handoff = last_note and last_note.get("type") == "handoff"

    if TRANSCRIPT_PATH.exists() and SESSION_PATH.exists():
        try:
            prev_session = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            prev_id = prev_session.get("session_id", "unknown")[:8]
        except (json.JSONDecodeError, KeyError):
            prev_id = "unknown"

        # Don't generate handoff for the current session's own transcript
        if prev_id != session_id[:8]:
            if last_is_handoff:
                # Consolidate: read transcript + existing handoff, update in place
                handoff_id = last_note["id"]
                handoff_content = last_note.get("content", "")
                contexts.append(
                    f"[scribe] Before proceeding with the user's request, spawn a subagent to:\n"
                    f"  1. Read {TRANSCRIPT_PATH} (stripped conversation transcript from session {prev_id})\n"
                    f"  2. Read the existing handoff note #{handoff_id}: {handoff_content}\n"
                    f"  3. Consolidate both into a single summary: what was done, what's pending, where it stopped\n"
                    f"  4. Update the handoff: python {APIS_DIR}/scribe/notes.py update {handoff_id} "
                    f'--content "<consolidated summary>"\n'
                    f"After the subagent finishes, read the notes to get context."
                )
            else:
                # No existing handoff at tail — create a new one
                contexts.append(
                    f"[scribe] Before proceeding with the user's request, spawn a subagent to:\n"
                    f"  1. Read {TRANSCRIPT_PATH} (stripped conversation transcript from session {prev_id})\n"
                    f"  2. Summarize the session: what was done, what's pending, where it stopped\n"
                    f"  3. Write the handoff: python {APIS_DIR}/scribe/notes.py add "
                    f"--type handoff --session-id {prev_id} --auto "
                    f'--content "<summary>"\n'
                    f"After the subagent finishes, read the notes to get context."
                )

    # Inject last handoff if it exists
    handoffs = [n for n in notes if n.get("type") == "handoff"]
    if handoffs:
        last_handoff = handoffs[-1]
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
