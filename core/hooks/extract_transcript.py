#!/usr/bin/env python3
"""
Extracts user + assistant text messages from a raw Claude session transcript.

Usage: python extract_transcript.py <transcript_path>
Output: JSONL to stdout — one {"role": ..., "text": ...} per line.
"""
import sys
import json
from pathlib import Path


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


STARTUP_NOISE_MARKERS = [
    "Launch a startup agent to handle session initialization",
    "You are a session startup agent",
]


def _is_startup_noise(text):
    """Detect startup skill expansion and boilerplate messages."""
    for marker in STARTUP_NOISE_MARKERS:
        if marker in text:
            return True
    return False


def extract_conversation(session_entries):
    """Extract user and assistant text messages, stripping tool calls/results."""
    messages = []
    for entry in session_entries:
        msg = entry.get("message", {})
        role = msg.get("role", "")

        if role not in ("user", "assistant"):
            continue

        content = msg.get("content", [])
        if isinstance(content, str) and content.strip():
            text = content.strip()
            if not _is_startup_noise(text):
                messages.append({"role": role, "text": text})
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(t for t in texts if t).strip()
            if text and not _is_startup_noise(text):
                messages.append({"role": role, "text": text})

    return messages


def main():
    if len(sys.argv) != 2:
        print("Usage: python extract_transcript.py <transcript_path>", file=sys.stderr)
        sys.exit(1)

    transcript_path = sys.argv[1]
    entries = read_session_jsonl(transcript_path)
    conversation = extract_conversation(entries)

    for msg in conversation:
        print(json.dumps(msg))


if __name__ == "__main__":
    main()
