#!/usr/bin/env python3
"""Bridge a refiner handoff scribe note into a runner intake file.

The `/refine` skill saves an approved handoff as a scribe note of type
`context` whose body follows a fixed markdown structure (`## Goal`, `## Shape`,
`## Behavior`, `## Boundaries`, `## Acceptance criteria`). This entry point
reads such a note, parses the sections, maps them to the runner intake schema,
and writes a validated intake JSON.

DEPRECATED ENTRY POINT — a thin shim over ``python -m runner.ticket
from-note``, kept for one release so existing scripts keep working. The logic
lives in runner/ticket.py.

Usage:
    refine_to_intake.py --note <note_id> --title "Short title"
    refine_to_intake.py --note <note_id> --title "..." --backlog
    refine_to_intake.py --note <note_id> --title "..." --explore-hints "scribe/notes.py,core/startup.py"
"""

import argparse
import sys

from .ticket import add_from_note_args, cmd_from_note


def main():
    parser = argparse.ArgumentParser(
        description="Bridge a refiner handoff scribe note into a runner intake file"
    )
    add_from_note_args(parser)
    args = parser.parse_args()
    sys.exit(cmd_from_note(args, parser))


if __name__ == "__main__":
    main()
