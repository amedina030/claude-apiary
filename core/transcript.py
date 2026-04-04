"""Read first user message from a Claude session transcript (JSONL)."""
import json
import time
from pathlib import Path
from typing import Optional


def read_first_message(transcript_path: str, retries: int = 1, delay: float = 0.1) -> str:
    """Return the text content of the first user message in the transcript.

    Reads the JSONL transcript and finds the first entry with type=="user".
    If the file doesn't exist or has no user message yet, retries once
    after ``delay`` seconds (the transcript may not be flushed on the
    very first tool call).

    Returns empty string on failure (identity defaults to user/general).
    """
    for attempt in range(1 + retries):
        content = _try_read(transcript_path)
        if content is not None:
            return content
        if attempt < retries:
            time.sleep(delay)
    return ""


def _try_read(transcript_path) -> Optional[str]:
    p = Path(transcript_path)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "user":
                    msg = entry.get("message", {})
                    return _extract_text(msg)
    except OSError:
        return None
    return None


def _extract_text(msg):
    """Extract plain text from a message object's content field."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)
