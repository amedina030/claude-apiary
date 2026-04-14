#!/usr/bin/env python3
"""
Extracts user + assistant text messages from a raw Claude session transcript.

Usage: python extract_transcript.py <transcript_path> [flags]

Flags (to keep output under tool Read limits):
  --summary        Emit a stats header only: message count, role breakdown,
                   plus first 3 and last 3 messages (truncated). No full body.
  --head N         Emit only the first N messages.
  --tail N         Emit only the last N messages.
  --max-chars N    Truncate each message's text to N chars (default: no truncation).

Output: JSONL to stdout — one {"role": ..., "text": ...} per line.
        With --summary, emits a single {"type": "summary", ...} line followed
        by up to 6 sample {"role": ..., "text": ...} lines.
"""
import argparse
import json
import sys
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


def _truncate(msg, max_chars):
    if max_chars is None or len(msg["text"]) <= max_chars:
        return msg
    return {"role": msg["role"], "text": msg["text"][:max_chars] + "...[truncated]"}


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("transcript_path")
    parser.add_argument("--summary", action="store_true",
                        help="Emit stats header + first/last 3 messages only")
    parser.add_argument("--head", type=int, default=None,
                        help="Emit only first N messages")
    parser.add_argument("--tail", type=int, default=None,
                        help="Emit only last N messages")
    parser.add_argument("--max-chars", type=int, default=None,
                        help="Truncate each message's text to N chars")
    args = parser.parse_args()

    entries = read_session_jsonl(args.transcript_path)
    conversation = extract_conversation(entries)

    if args.summary:
        user_count = sum(1 for m in conversation if m["role"] == "user")
        asst_count = sum(1 for m in conversation if m["role"] == "assistant")
        total_chars = sum(len(m["text"]) for m in conversation)
        header = {
            "type": "summary",
            "total_messages": len(conversation),
            "user_messages": user_count,
            "assistant_messages": asst_count,
            "total_chars": total_chars,
        }
        print(json.dumps(header))
        samples = conversation[:3] + conversation[-3:] if len(conversation) > 6 else conversation
        for msg in samples:
            print(json.dumps(_truncate(msg, args.max_chars or 500)))
        return

    if args.head is not None:
        conversation = conversation[:args.head]
    if args.tail is not None:
        conversation = conversation[-args.tail:]

    for msg in conversation:
        print(json.dumps(_truncate(msg, args.max_chars)))


if __name__ == "__main__":
    main()
