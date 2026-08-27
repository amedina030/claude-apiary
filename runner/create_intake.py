#!/usr/bin/env python3
"""
Create an intake file for the autonomous runner.

Generates a UUID-keyed JSON file at `<state>/runner/intake/<uuid>.json`
containing all seed context needed for autonomous refinement.

Supports two modes:
  1. Direct flags: --title, --problem, --description, --scope [--context]
  2. From scribe TODO: --from-todo <id> (maps TODO content to description;
     title, problem, scope still required as flags)

After writing, the file is validated; on validation failure it is deleted.

DEPRECATED ENTRY POINT — a thin shim over ``python -m runner.ticket
create-intake``, kept for one release so existing scripts keep working. The
logic lives in runner/ticket.py.

Usage:
    create_intake.py --title "..." --problem "..." --description "..." --scope "..."
    create_intake.py --from-todo 134 --title "..." --problem "..." --scope "..."
"""
import argparse
import sys

from .ticket import add_ticket_content_args, cmd_create_intake


def main():
    parser = argparse.ArgumentParser(description="Create runner intake file")
    add_ticket_content_args(parser, explore_hints=True)
    args = parser.parse_args()
    sys.exit(cmd_create_intake(args, parser))


if __name__ == "__main__":
    main()
