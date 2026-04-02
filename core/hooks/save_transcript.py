#!/usr/bin/env python3
"""
Stop hook — saves a stripped conversation transcript for handoff generation.

Extracts user + assistant text messages from the session transcript,
writes to ~/.claude/.last-transcript.jsonl. Overwrites each time.
Also writes session metadata to ~/.claude/.last-session.json.
"""
import sys
import json
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
TRANSCRIPT_PATH = CLAUDE_DIR / ".last-transcript.jsonl"
SESSION_PATH = CLAUDE_DIR / ".last-session.json"


def extract_conversation(session_entries):
    """Extract user and assistant text messages, stripping tool calls/results."""
    messages = []
    for entry in session_entries:
        msg = entry.get("message", {})
        role = msg.get("role", "")

        if role == "user":
            content = msg.get("content", [])
            if isinstance(content, str) and content.strip():
                messages.append({"role": "user", "text": content.strip()})
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = " ".join(t for t in texts if t).strip()
                if text:
                    messages.append({"role": "user", "text": text})

        elif role == "assistant":
            content = msg.get("content", [])
            if isinstance(content, str) and content.strip():
                messages.append({"role": "assistant", "text": content.strip()})
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = " ".join(t for t in texts if t).strip()
                if text:
                    messages.append({"role": "assistant", "text": text})

    return messages


def read_session_jsonl(path):
    if not path or not Path(path).exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    if not session_id or not transcript_path:
        sys.exit(0)

    # Extract conversation
    session_entries = read_session_jsonl(transcript_path)
    conversation = extract_conversation(session_entries)

    if not conversation:
        sys.exit(0)

    # Write stripped transcript
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRANSCRIPT_PATH, "w", encoding="utf-8") as f:
        for msg in conversation:
            f.write(json.dumps(msg) + "\n")

    # Write session metadata
    SESSION_PATH.write_text(
        json.dumps({"session_id": session_id, "transcript_path": transcript_path}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
