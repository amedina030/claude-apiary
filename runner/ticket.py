#!/usr/bin/env python3
"""One CLI for the runner's ticket lifecycle.

Five separate scripts used to own one workflow — draft a backlog ticket,
promote it to intake, create an intake directly, bridge a `/refine` handoff
note into either, validate the result. Between them they carried three
`slugify` implementations, three `read_todo`/`read_note` implementations, and
three places that shelled out to `python -m runner.validate_intake` instead of
importing its `validate()` (review X-3, `docs/review/subsystems/runner.md`
§2/§8). There is one of each here.

Usage:
    python -m runner.ticket draft --title "..." --problem "..." --description "..." --scope "..."
    python -m runner.ticket create-intake --title "..." --problem "..." --description "..." --scope "..."
    python -m runner.ticket promote <slug>
    python -m runner.ticket from-note --note <id> --title "..."
    python -m runner.ticket validate <path>

The old module entry points (`runner.create_intake`, `runner.draft_ticket`,
`runner.promote`, `runner.refine_to_intake`) still work: they are thin shims
over these handlers, kept for one release so scripts and muscle memory don't
break mid-flight.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path

from .detached_lib import slugify
from .stage_lib import is_uuid_safe, iter_unique
from .target_repo import backlog_dir, intake_dir
from .validate_intake import validate as validate_intake_data

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
NOTES_SCRIPT = REPO_ROOT / "scribe" / "notes.py"

# Resolved at import so tests can patch them, matching the convention the
# five originals used.
INTAKE_DIR = intake_dir()
BACKLOG_DIR = backlog_dir()

TICKET_SLUG_MAX = 60

REQUIRED_SECTIONS = ["Goal", "Shape", "Behavior", "Boundaries", "Acceptance criteria"]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def ticket_slug(title: str) -> str:
    """Filename slug for a backlog ticket (capped, empty allowed).

    Same slugifier the run branch uses, capped at a filename-friendly length.
    An empty result is returned as-is so callers can reject it with a message
    about the title rather than silently writing `item.json`.
    """
    return slugify(title, max_length=TICKET_SLUG_MAX, fallback="")


def read_note(note_id: str) -> str:
    """Return a scribe note's body via `notes.py get`, or exit 1.

    The body is everything after the `---` metadata separator; if the output
    has no separator the whole thing is treated as the body.
    """
    result = subprocess.run(
        [sys.executable, str(NOTES_SCRIPT), "get", str(note_id)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        print(f"Note {note_id} not found", file=sys.stderr)
        sys.exit(1)

    lines = (result.stdout or "").splitlines()
    try:
        sep_index = lines.index("---")
        content = "\n".join(lines[sep_index + 1 :]).strip()
    except ValueError:
        content = (result.stdout or "").strip()

    if not content:
        print(f"Note {note_id} has no content", file=sys.stderr)
        sys.exit(1)
    return content


def parse_hints(csv: str) -> list:
    """Split a comma-separated hint list, dropping empties and duplicates."""
    return iter_unique(h.strip() for h in (csv or "").split(","))


def write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def validate_intake_file(path: Path) -> list:
    """Validate an intake file on disk. Returns error strings (empty = valid).

    Imports `validate()` rather than spawning `python -m runner.validate_intake`,
    which is what create_intake, promote and refine_to_intake each did — three
    interpreter starts for a pure function in the same package.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"could not read {path}: {e}"]
    return validate_intake_data(data)


def _require_fields(args, description, *, parser=None) -> None:
    """Exit unless title/problem/description/scope are all present."""
    missing = []
    if not args.title:
        missing.append("--title")
    if not args.problem:
        missing.append("--problem")
    if not description:
        missing.append("--description")
    if not args.scope:
        missing.append("--scope")
    if not missing:
        return
    if parser is not None:
        parser.error(f"the following arguments are required: {', '.join(missing)}")
    print(f"Missing required fields: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)


def _seed_from_note(args) -> tuple:
    """Resolve (description, source) from --from-todo, if given."""
    if not getattr(args, "from_todo", None):
        return args.description, None
    content = read_note(args.from_todo)
    description = args.description if args.description is not None else content
    return description, str(args.from_todo)


def _base_record(args, description: str, source, record_id: str) -> dict:
    record = {
        "id": record_id,
        "title": args.title,
        "problem": args.problem,
        "description": description,
        "scope": args.scope,
        "context": getattr(args, "context", "") or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if source is not None:
        record["source"] = source
    hints = parse_hints(getattr(args, "explore_hints", "") or "")
    if hints:
        record["explore_hints"] = hints
    return record


# --------------------------------------------------------------------------- #
# Handoff-note parsing (was refine_to_intake)
# --------------------------------------------------------------------------- #


def parse_sections(text: str) -> dict:
    """Split a refiner handoff body into a {section_name: body_text} dict.

    Section headers are lines starting with `## `. Body is every line until the
    next `## ` header.
    """
    sections: dict = {}
    current = None
    buf: list = []
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


def extract_field(section_body: str, label: str):
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
    ValueError naming the missing/empty required sections.
    """
    missing = [s for s in REQUIRED_SECTIONS if not sections.get(s)]
    if missing:
        raise ValueError(f"handoff is missing sections: {', '.join(missing)}")

    problem = extract_field(sections["Goal"], "Problem")
    if not problem:
        raise ValueError("Goal section is missing a **Problem:** field")

    shape = sections["Shape"].strip()
    behavior = sections["Behavior"].strip()
    return {
        "problem": problem,
        "description": f"SHAPE:\n{shape}\n\nBEHAVIOR:\n{behavior}".strip(),
        "scope": sections["Boundaries"].strip(),
        "context": f"ACCEPTANCE CRITERIA:\n{sections['Acceptance criteria'].strip()}",
    }


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #


def cmd_draft(args, parser=None) -> int:
    """Write a backlog draft ticket at backlog/<slug>.json."""
    description, source = _seed_from_note(args)
    _require_fields(args, description, parser=parser)

    slug = ticket_slug(args.title)
    if not slug:
        print("Error: title produces an empty slug (remove special characters)", file=sys.stderr)
        return 1
    file_path = BACKLOG_DIR / f"{slug}.json"
    if file_path.exists():
        print(f"Error: backlog ticket {slug}.json already exists", file=sys.stderr)
        return 1

    ticket = _base_record(args, description, source, str(uuid_mod.uuid4()))
    write_record(file_path, ticket)
    print(str(file_path))
    print(ticket["id"])
    return 0


def cmd_create_intake(args, parser=None) -> int:
    """Write a validated intake file at intake/<uuid>.json."""
    description, source = _seed_from_note(args)
    _require_fields(args, description)

    intake_id = str(uuid_mod.uuid4())
    record = _base_record(args, description, source, intake_id)
    file_path = INTAKE_DIR / f"{intake_id}.json"
    write_record(file_path, record)

    errors = validate_intake_file(file_path)
    if errors:
        file_path.unlink(missing_ok=True)
        print("Validation failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1

    print(str(file_path))
    return 0


def cmd_promote(args, parser=None) -> int:
    """Move a backlog draft into intake, validating it on the way."""
    slug = args.slug
    if "/" in slug or not is_uuid_safe(slug):
        print("Error: invalid slug (path separators not allowed)", file=sys.stderr)
        return 1
    backlog_path = BACKLOG_DIR / f"{slug}.json"
    if not backlog_path.exists():
        print(f"Error: backlog ticket {slug}.json not found", file=sys.stderr)
        return 1

    data = json.loads(backlog_path.read_text(encoding="utf-8"))
    required_keys = ["title", "problem", "description", "scope", "created_at"]
    missing_keys = [k for k in required_keys if k not in data]
    if missing_keys:
        print(
            f"Error: backlog ticket is missing required fields: {', '.join(missing_keys)}",
            file=sys.stderr,
        )
        return 1

    intake_id = data.get("id") or str(uuid_mod.uuid4())
    record = {
        "id": intake_id,
        "title": data["title"],
        "problem": data["problem"],
        "description": data["description"],
        "scope": data["scope"],
        "context": data.get("context", ""),
        "created_at": data["created_at"],
    }
    for optional in ("source", "explore_hints", "target_repo"):
        if optional in data:
            record[optional] = data[optional]

    intake_path = INTAKE_DIR / f"{intake_id}.json"
    write_record(intake_path, record)

    errors = validate_intake_file(intake_path)
    if errors:
        intake_path.unlink(missing_ok=True)
        print("\n".join(errors), file=sys.stderr)
        return 1

    backlog_path.unlink()
    print(str(intake_path))
    print(intake_id)
    return 0


def cmd_from_note(args, parser=None) -> int:
    """Bridge a `/refine` handoff scribe note into an intake or backlog file."""
    body = read_note(args.note)
    sections = parse_sections(body)
    try:
        mapped = map_to_intake(sections)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    record_id = str(uuid_mod.uuid4())
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
        slug = ticket_slug(args.title)
        if not slug:
            print("Error: title produces an empty slug", file=sys.stderr)
            return 1
        file_path = BACKLOG_DIR / f"{slug}.json"
        if file_path.exists():
            print(f"Error: backlog ticket {slug}.json already exists", file=sys.stderr)
            return 1
        write_record(file_path, record)
        print(str(file_path))
        return 0

    file_path = INTAKE_DIR / f"{record_id}.json"
    write_record(file_path, record)
    errors = validate_intake_file(file_path)
    if errors:
        file_path.unlink(missing_ok=True)
        print("Validation failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print(str(file_path))
    return 0


def cmd_validate(args, parser=None) -> int:
    """Validate an intake JSON file already on disk."""
    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1
    errors = validate_intake_file(path)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print("Valid")
    return 0


# --------------------------------------------------------------------------- #
# Argument wiring — shared with the shim entry points
# --------------------------------------------------------------------------- #


def add_ticket_content_args(parser, *, explore_hints: bool) -> None:
    """The title/problem/description/scope block every creating command takes."""
    parser.add_argument("--title", help="Short title for the task")
    parser.add_argument("--problem", help="Problem statement (min 20 chars)")
    parser.add_argument("--description", help="Detailed description (min 20 chars)")
    parser.add_argument("--scope", help="What's in scope for this runner run")
    parser.add_argument("--context", default="", help="Additional context (optional)")
    parser.add_argument(
        "--from-todo", dest="from_todo", help="Scribe note ID to seed --description from"
    )
    if explore_hints:
        parser.add_argument(
            "--explore-hints",
            dest="explore_hints",
            default="",
            help="Comma-separated repo-relative paths the refiner should start "
            "with (optional; refiner can still branch out)",
        )


def add_promote_args(parser) -> None:
    parser.add_argument(
        "slug",
        help="Backlog ticket slug — the filename without directory or .json extension",
    )


def add_from_note_args(parser) -> None:
    parser.add_argument(
        "--note", required=True, help="Scribe note ID containing the refiner handoff"
    )
    parser.add_argument(
        "--title", required=True, help="Short title for the intake (refiner handoff has no title)"
    )
    parser.add_argument(
        "--backlog",
        action="store_true",
        help="Write to backlog/<slug>.json instead of intake/<uuid>.json",
    )
    parser.add_argument(
        "--explore-hints",
        dest="explore_hints",
        default="",
        help="Comma-separated repo-relative paths for the auto-refiner",
    )


def add_validate_args(parser) -> None:
    parser.add_argument("file", help="Path to intake JSON file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Runner ticket lifecycle: draft, promote, create, validate",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p_draft = subs.add_parser("draft", help="Create a backlog draft ticket")
    add_ticket_content_args(p_draft, explore_hints=False)
    p_draft.set_defaults(handler=cmd_draft)

    p_intake = subs.add_parser("create-intake", help="Create a validated intake file directly")
    add_ticket_content_args(p_intake, explore_hints=True)
    p_intake.set_defaults(handler=cmd_create_intake)

    p_promote = subs.add_parser("promote", help="Promote a backlog draft to intake")
    add_promote_args(p_promote)
    p_promote.set_defaults(handler=cmd_promote)

    p_note = subs.add_parser(
        "from-note", help="Bridge a /refine handoff note into intake or backlog"
    )
    add_from_note_args(p_note)
    p_note.set_defaults(handler=cmd_from_note)

    p_validate = subs.add_parser("validate", help="Validate an intake JSON file")
    add_validate_args(p_validate)
    p_validate.set_defaults(handler=cmd_validate)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.handler(args, parser))


if __name__ == "__main__":
    main()
