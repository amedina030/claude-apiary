#!/usr/bin/env python3
"""
Manage clarifier session log files.

Usage:
  python write_log.py [<payload.json>]           # init: start a new session
  python write_log.py --append [<round.json>]    # complete last round + add next questions
  python write_log.py --complete [<final.json>]  # finalize the session

Reads from stdin if no file argument is given.

init JSON schema:
  cwd: str                     (optional — used to auto-detect log_dir)
  log_dir: str                 (optional — overrides cwd detection)
  original_prompt: str
  interpretation: str
  detected_ambiguities: list[str]
  intended_plan: str
  first_questions: str         (questions asked at the end of Round 1)

append JSON schema:
  responses: str               (user's answers to the previous round's questions)
  updated_prompt: str          (prompt after incorporating responses)
  new_questions: str           (questions for the next round)
  cwd: str                     (optional — helps locate .current if log_dir unknown)

complete JSON schema:
  responses: str               (user's final answers — omit if no pending questions)
  final_prompt: str
  outcome: str                 ("Fully resolved" | "Approved with unresolved ambiguities" | "User granted permission to proceed")
  unresolved_ambiguities: str  (description or "None")
  cwd: str                     (optional)

Output (stdout):
  uuid: <session-uuid>
  log: <filename>
  path: <full-path-to-md>
  round_count: <N>             (number of completed rounds — rounds with actual user responses)
  status: in_progress | complete

State files written:
  <log_dir>/<filename>.md      human-readable markdown log
  <log_dir>/<filename>.json    machine-readable session state
  <log_dir>/.current           pointer to the active session (deleted on complete)
"""

import argparse
import datetime
import json
import os
import random
import sys
import uuid
from pathlib import Path


CURRENT_POINTER = ".current"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_uuid() -> str:
    return str(uuid.uuid4())


def generate_filename(ts: str) -> str:
    suffix = "".join(random.choices("0123456789abcdef", k=4))
    ts_slug = ts.replace(" ", "-").replace(":", "")
    return f"clarifier-{ts_slug}-{suffix}.md"


def determine_log_dir(cwd: str | None, explicit: str | None) -> str:
    if explicit:
        return os.path.expanduser(explicit)
    if cwd:
        project_claude = Path(cwd) / ".claude"
        if project_claude.is_dir():
            return str(project_claude / "clarifier-logs")
    return str(Path.home() / ".claude" / "clarifier-logs")


def load_json(path: str | None) -> dict:
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return json.load(sys.stdin)


def count_completed_rounds(rounds: list) -> int:
    """Count rounds where the user has actually responded."""
    return sum(
        1 for r in rounds
        if r.get("responses") not in ("[pending]", None, "")
    )


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------

def state_file_path(log_dir: str, md_filename: str) -> Path:
    return Path(log_dir) / (Path(md_filename).stem + ".json")


def write_state(log_dir: str, state: dict):
    path = state_file_path(log_dir, state["filename"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def find_log_dir_with_current(cwd: str | None, explicit: str | None) -> str:
    """Return the log_dir that contains .current, trying cwd-based dir first."""
    primary = determine_log_dir(cwd, explicit)
    if (Path(primary) / CURRENT_POINTER).exists():
        return primary
    fallback = str(Path.home() / ".claude" / "clarifier-logs")
    if (Path(fallback) / CURRENT_POINTER).exists():
        return fallback
    raise FileNotFoundError(
        f"No active clarifier session found in {primary} or {fallback}. Run init first."
    )


def load_current_state(log_dir: str) -> dict:
    pointer = Path(log_dir) / CURRENT_POINTER
    ref = json.loads(pointer.read_text(encoding="utf-8"))
    sp = state_file_path(log_dir, ref["filename"])
    if not sp.exists():
        raise FileNotFoundError(f"Session state file not found: {sp}")
    with open(sp, encoding="utf-8") as f:
        return json.load(f)


def write_current_pointer(log_dir: str, session_uuid: str, filename: str):
    pointer = Path(log_dir) / CURRENT_POINTER
    pointer.write_text(
        json.dumps({"uuid": session_uuid, "filename": filename}),
        encoding="utf-8",
    )


def delete_current_pointer(log_dir: str):
    pointer = Path(log_dir) / CURRENT_POINTER
    if pointer.exists():
        pointer.unlink()


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def format_rounds_md(rounds: list) -> str:
    if not rounds:
        return "[none]"
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


def render_markdown(state: dict) -> str:
    status = "Complete" if state.get("status") == "complete" else "In Progress"
    return f"""# Clarifier Session Log
Date: {state.get('timestamp', '')}
ID: {state.get('uuid', '')}
Working directory: {state.get('cwd', '')}
Status: {status}

## Original Prompt
{state.get('original_prompt', '')}

## Executing Agent Input
**Interpretation:** {state.get('interpretation', '')}
**Detected ambiguities:**
{format_ambiguities(state.get('detected_ambiguities', []))}
**Intended plan:** {state.get('intended_plan', '')}

## Clarification Rounds

{format_rounds_md(state.get('rounds', []))}

## Unresolved Ambiguities
{state.get('unresolved_ambiguities', '[pending]')}

## Final Approved Prompt
{state.get('final_prompt', '[pending]')}

## Outcome
{state.get('outcome', '[pending]')}
"""


def write_markdown(log_dir: str, state: dict):
    log_path = Path(log_dir) / state["filename"]
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(state))


def print_output(state: dict, log_dir: str):
    completed = count_completed_rounds(state.get("rounds", []))
    log_path = Path(log_dir) / state["filename"]
    print(f"uuid: {state['uuid']}")
    print(f"log: {state['filename']}")
    print(f"path: {log_path}")
    print(f"round_count: {completed}")
    print(f"status: {state.get('status', 'in_progress')}")


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------

def cmd_init(data: dict):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session_uuid = generate_uuid()
    filename = generate_filename(ts)
    log_dir = determine_log_dir(data.get("cwd"), data.get("log_dir"))

    Path(log_dir).mkdir(parents=True, exist_ok=True)

    state = {
        "uuid": session_uuid,
        "filename": filename,
        "log_dir": log_dir,
        "timestamp": ts,
        "cwd": data.get("cwd", ""),
        "status": "in_progress",
        "original_prompt": data.get("original_prompt", ""),
        "interpretation": data.get("interpretation", ""),
        "detected_ambiguities": data.get("detected_ambiguities", []),
        "intended_plan": data.get("intended_plan", ""),
        "rounds": [
            {
                "questions": data.get("first_questions", ""),
                "responses": "[pending]",
                "updated_prompt": "[pending]",
            }
        ],
        "unresolved_ambiguities": "[pending]",
        "final_prompt": "[pending]",
        "outcome": "[pending]",
    }

    write_state(log_dir, state)
    write_markdown(log_dir, state)
    write_current_pointer(log_dir, session_uuid, filename)
    print_output(state, log_dir)


def cmd_append_round(data: dict):
    log_dir = find_log_dir_with_current(data.get("cwd"), data.get("log_dir"))
    state = load_current_state(log_dir)

    # Complete the last round with user's responses
    if state["rounds"]:
        last = state["rounds"][-1]
        last["responses"] = data.get("responses", "[pending]")
        last["updated_prompt"] = data.get("updated_prompt", "[pending]")

    # Open the next round
    state["rounds"].append({
        "questions": data.get("new_questions", ""),
        "responses": "[pending]",
        "updated_prompt": "[pending]",
    })

    write_state(log_dir, state)
    write_markdown(log_dir, state)
    print_output(state, log_dir)


def cmd_complete(data: dict):
    log_dir = find_log_dir_with_current(data.get("cwd"), data.get("log_dir"))
    state = load_current_state(log_dir)

    # Complete the last round if responses are provided
    if state["rounds"] and data.get("responses"):
        last = state["rounds"][-1]
        last["responses"] = data["responses"]
        last["updated_prompt"] = data.get("final_prompt", "[pending]")

    state["status"] = "complete"
    state["final_prompt"] = data.get("final_prompt", "[pending]")
    state["outcome"] = data.get("outcome", "[pending]")
    state["unresolved_ambiguities"] = data.get("unresolved_ambiguities", "None")

    write_state(log_dir, state)
    write_markdown(log_dir, state)
    delete_current_pointer(log_dir)
    print_output(state, log_dir)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Manage clarifier session logs.")
    parser.add_argument("file", nargs="?", help="JSON payload file (reads stdin if omitted)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--append", action="store_true", help="Append round to active session")
    mode.add_argument("--complete", action="store_true", help="Finalize active session")
    args = parser.parse_args()

    data = load_json(args.file)

    if args.append:
        cmd_append_round(data)
    elif args.complete:
        cmd_complete(data)
    else:
        cmd_init(data)


if __name__ == "__main__":
    main()
