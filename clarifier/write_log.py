#!/usr/bin/env python3
"""Write a clarifier session log file.

Reads JSON from a file path (argv[1]) or stdin if no argument given.
Generates UUID and filename if not provided in the input.

Output (stdout):
    uuid: <session-uuid>
    log: <filename>
    path: <full-path>

JSON schema:
{
  "log_dir": str,             # supports ~ expansion (required)
  "uuid": str,                # optional — generated if absent
  "filename": str,            # optional — generated if absent (requires uuid absent too)
  "timestamp": str,           # optional — generated if absent
  "cwd": str,                 # optional
  "status": "in_progress" | "complete",
  "original_prompt": str,
  "interpretation": str,
  "detected_ambiguities": list[str],
  "intended_plan": str,
  "rounds": [
    {
      "questions": str,
      "responses": str,       # "[pending]" if not yet answered
      "updated_prompt": str   # "[pending]" if not yet updated
    }
  ],
  "unresolved_ambiguities": str,
  "final_prompt": str,
  "outcome": str
}
"""

import datetime
import json
import os
import random
import sys
import uuid


def generate_uuid() -> str:
    return str(uuid.uuid4())


def generate_filename(ts: str) -> str:
    suffix = "".join(random.choices("0123456789abcdef", k=4))
    # ts format: "2026-03-14 21:40:00" -> "2026-03-14-214000"
    ts_slug = ts.replace(" ", "-").replace(":", "")
    return f"clarifier-{ts_slug}-{suffix}.md"


def format_rounds(rounds: list) -> str:
    if not rounds:
        return "[pending]"
    parts = []
    for i, r in enumerate(rounds, 1):
        parts.append(f"### Round {i}")
        parts.append(f"**Questions asked:**\n{r.get('questions', '[pending]')}")
        parts.append(f"**User responses:**\n{r.get('responses', '[pending]')}")
        parts.append(f"**Prompt after this round:**\n{r.get('updated_prompt', '[pending]')}")
    return "\n\n".join(parts)


def format_ambiguities(items) -> str:
    if isinstance(items, list):
        return "\n".join(f"- {a}" for a in items) if items else "None"
    return str(items)


def format_log(data: dict) -> str:
    status = "In Progress" if data.get("status") == "in_progress" else "Complete"
    return f"""# Clarifier Session Log
Date: {data.get('timestamp', '')}
ID: {data.get('uuid', '')}
Working directory: {data.get('cwd', '')}
Status: {status}

## Original Prompt
{data.get('original_prompt', '')}

## Executing Agent Input
**Interpretation:** {data.get('interpretation', '')}
**Detected ambiguities:**
{format_ambiguities(data.get('detected_ambiguities', []))}
**Intended plan:** {data.get('intended_plan', '')}

## Clarification Rounds

{format_rounds(data.get('rounds', []))}

## Unresolved Ambiguities
{data.get('unresolved_ambiguities', '[pending]')}

## Final Approved Prompt
{data.get('final_prompt', '[pending]')}

## Outcome
{data.get('outcome', '[pending]')}
"""


def main():
    if len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    # Generate timestamp, UUID, filename if not provided
    if "timestamp" not in data:
        data["timestamp"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "uuid" not in data:
        data["uuid"] = generate_uuid()
    if "filename" not in data:
        data["filename"] = generate_filename(data["timestamp"])

    log_dir = os.path.expanduser(data["log_dir"])
    os.makedirs(log_dir, exist_ok=True)

    log_path = os.path.join(log_dir, data["filename"])
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(format_log(data))

    print(f"uuid: {data['uuid']}")
    print(f"log: {data['filename']}")
    print(f"path: {log_path}")


if __name__ == "__main__":
    main()
