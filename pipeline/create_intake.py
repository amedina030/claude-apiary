#!/usr/bin/env python3
"""
Create an intake file for the autonomous pipeline.

Generates a UUID-keyed JSON file at pipeline/intake/<uuid>.json containing
all seed context needed for autonomous refinement.

Supports two modes:
  1. Direct flags: --title, --problem, --description, --scope [--context]
  2. From scribe TODO: --from-todo <id> (maps TODO content to description;
     title, problem, scope still required as flags)

After writing, runs validate_intake.py to confirm the file is valid.
On validation failure the file is deleted.

Usage:
    create_intake.py --title "..." --problem "..." --description "..." --scope "..."
    create_intake.py --from-todo 134 --title "..." --problem "..." --scope "..."
"""
import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
INTAKE_DIR = SCRIPT_DIR / "intake"
VALIDATE_SCRIPT = SCRIPT_DIR / "validate_intake.py"
NOTES_SCRIPT = SCRIPT_DIR.parent / "scribe" / "notes.py"


def read_todo(todo_id: str) -> str:
    """Read a scribe TODO by ID and return its content text."""
    result = subprocess.run(
        [sys.executable, str(NOTES_SCRIPT), "get", str(todo_id)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"TODO {todo_id} not found", file=sys.stderr)
        sys.exit(1)

    # Parse output: metadata lines, then "---", then content
    lines = result.stdout.splitlines()
    try:
        sep_index = lines.index("---")
        content = "\n".join(lines[sep_index + 1:]).strip()
    except ValueError:
        content = result.stdout.strip()

    if not content:
        print(f"TODO {todo_id} has no content", file=sys.stderr)
        sys.exit(1)

    return content


def main():
    parser = argparse.ArgumentParser(description="Create pipeline intake file")
    parser.add_argument("--from-todo", dest="from_todo", help="Scribe TODO ID to seed from")
    parser.add_argument("--title", help="Short title for the task")
    parser.add_argument("--problem", help="Problem statement (min 20 chars)")
    parser.add_argument("--description", help="Detailed description (min 20 chars)")
    parser.add_argument("--scope", help="What's in scope for this pipeline run")
    parser.add_argument("--context", default="", help="Additional context (optional)")
    parser.add_argument(
        "--explore-hints",
        dest="explore_hints",
        default="",
        help="Comma-separated repo-relative paths the refiner should start with "
             "(optional; refiner can still branch out)",
    )
    args = parser.parse_args()

    # If --from-todo, read TODO content as default description
    todo_content = None
    source = None
    if args.from_todo:
        todo_content = read_todo(args.from_todo)
        source = str(args.from_todo)

    # Resolve description: flag wins over TODO content
    description = args.description
    if description is None and todo_content is not None:
        description = todo_content

    # Check required fields
    missing = []
    if not args.title:
        missing.append("--title")
    if not args.problem:
        missing.append("--problem")
    if not description:
        missing.append("--description")
    if not args.scope:
        missing.append("--scope")
    if missing:
        print(f"Missing required fields: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    # Build intake object
    intake_id = str(uuid.uuid4())
    intake = {
        "id": intake_id,
        "title": args.title,
        "problem": args.problem,
        "description": description,
        "scope": args.scope,
        "context": args.context,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if source is not None:
        intake["source"] = source

    # Optional explore_hints: split CSV, drop empties, dedupe, preserve order.
    if args.explore_hints.strip():
        seen = set()
        hints = []
        for h in args.explore_hints.split(","):
            h = h.strip()
            if h and h not in seen:
                seen.add(h)
                hints.append(h)
        if hints:
            intake["explore_hints"] = hints

    # Write file
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    file_path = INTAKE_DIR / f"{intake_id}.json"
    file_path.write_text(json.dumps(intake, indent=2), encoding="utf-8")

    # Validate
    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(file_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        file_path.unlink(missing_ok=True)
        print(f"Validation failed:\n{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    print(str(file_path))


if __name__ == "__main__":
    main()
