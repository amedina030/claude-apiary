#!/usr/bin/env python3
"""Bridge a refiner handoff scribe note into a runner intake file.

The `/refine` skill saves an approved handoff as a scribe note of type
`context` whose body follows a fixed markdown structure (`## Goal`, `## Shape`,
`## Behavior`, `## Boundaries`, `## Acceptance criteria`). This script reads
such a note, parses the sections, maps them to the runner intake schema, and
writes a validated intake JSON under `runner/intake/`.

Usage:
    refine_to_intake.py --note <note_id> --title "Short title"
    refine_to_intake.py --note <note_id> --title "..." --backlog
    refine_to_intake.py --note <note_id> --title "..." --explore-hints "scribe/notes.py,core/startup.py"
"""
import argparse
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runner.target_repo import backlog_dir, intake_dir

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INTAKE_DIR = intake_dir()
BACKLOG_DIR = backlog_dir()
NOTES_SCRIPT = REPO_ROOT / "scribe" / "notes.py"

REQUIRED_SECTIONS = ["Goal", "Shape", "Behavior", "Boundaries", "Acceptance criteria"]


def read_note(note_id: str) -> str:
    """Read a scribe note's body via `notes.py get`. Returns the body text
    (everything after the `---` metadata separator)."""
    result = subprocess.run(
        [sys.executable, str(NOTES_SCRIPT), "get", str(note_id)],
        capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        print(f"Note {note_id} not found", file=sys.stderr)
        sys.exit(1)

    lines = result.stdout.splitlines()
    try:
        sep_index = lines.index("---")
        content = "\n".join(lines[sep_index + 1:]).strip()
    except ValueError:
        content = result.stdout.strip()

    if not content:
        print(f"Note {note_id} has no body content", file=sys.stderr)
        sys.exit(1)
    return content


def parse_sections(text: str) -> dict:
    """Split a refiner handoff body into a {section_name: body_text} dict.

    Section headers are lines starting with `## `. Body is every line until the
    next `## ` header.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def extract_field(section_body: str, label: str) -> str | None:
    """Extract the value following a `**Label:**` marker within a section.

    Returns the text from after the marker up to (but not including) the next
    `**Label:**` marker or the end of the section. Strips whitespace.
    """
    pattern = re.compile(
        rf"\*\*{re.escape(label)}:\*\*\s*(.*?)(?=\n\s*-?\s*\*\*[A-Za-z][^*]*:\*\*|\Z)",
        re.DOTALL,
    )
    match = pattern.search(section_body)
    if not match:
        return None
    return match.group(1).strip()


def map_to_intake(sections: dict) -> dict:
    """Map parsed handoff sections onto the intake field shape.

    Returns a dict with keys problem, description, scope, context. Raises
    ValueError with a list of missing/empty required sections.
    """
    missing = [s for s in REQUIRED_SECTIONS if not sections.get(s)]
    if missing:
        raise ValueError(f"handoff is missing sections: {', '.join(missing)}")

    goal = sections["Goal"]
    problem = extract_field(goal, "Problem")
    if not problem:
        raise ValueError("Goal section is missing a **Problem:** field")

    shape = sections["Shape"].strip()
    behavior = sections["Behavior"].strip()
    description = f"SHAPE:\n{shape}\n\nBEHAVIOR:\n{behavior}".strip()

    scope = sections["Boundaries"].strip()
    context = f"ACCEPTANCE CRITERIA:\n{sections['Acceptance criteria'].strip()}"

    return {
        "problem": problem,
        "description": description,
        "scope": scope,
        "context": context,
    }


def slugify(title: str) -> str:
    lowered = title.lower()
    result = re.sub(r'[^a-z0-9-]', '-', lowered)
    result = re.sub(r'-+', '-', result)
    return result.strip('-')[:60].strip('-')


def parse_hints(csv: str) -> list[str]:
    seen = set()
    out = []
    for h in csv.split(","):
        h = h.strip()
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Bridge a refiner handoff scribe note into a runner intake file"
    )
    parser.add_argument("--note", required=True, help="Scribe note ID containing the refiner handoff")
    parser.add_argument("--title", required=True, help="Short title for the intake (refiner handoff has no title)")
    parser.add_argument(
        "--backlog",
        action="store_true",
        help="Write to runner/backlog/<slug>.json instead of runner/intake/<uuid>.json",
    )
    parser.add_argument(
        "--explore-hints",
        dest="explore_hints",
        default="",
        help="Comma-separated repo-relative paths for the auto-refiner",
    )
    args = parser.parse_args()

    body = read_note(args.note)
    sections = parse_sections(body)

    try:
        mapped = map_to_intake(sections)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    record_id = str(uuid.uuid4())
    record = {
        "id": record_id,
        "title": args.title,
        "problem": mapped["problem"],
        "description": mapped["description"],
        "scope": mapped["scope"],
        "context": mapped["context"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": f"scribe-note:{args.note}",
    }

    hints = parse_hints(args.explore_hints)
    if hints:
        record["explore_hints"] = hints

    if args.backlog:
        slug = slugify(args.title)
        if not slug:
            print("Error: title produces an empty slug", file=sys.stderr)
            sys.exit(1)
        BACKLOG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = BACKLOG_DIR / f"{slug}.json"
        if file_path.exists():
            print(f"Error: backlog ticket {slug}.json already exists", file=sys.stderr)
            sys.exit(1)
        file_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(str(file_path))
        return

    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    file_path = INTAKE_DIR / f"{record_id}.json"
    file_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "runner.validate_intake", str(file_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        file_path.unlink(missing_ok=True)
        print(f"Validation failed:\n{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    print(str(file_path))


if __name__ == "__main__":
    main()
