#!/usr/bin/env python3
"""Create a backlog draft ticket for the runner.

Writes a JSON file to `<state>/runner/backlog/<slug>.json`.

DEPRECATED ENTRY POINT — a thin shim over ``python -m runner.ticket draft``,
kept for one release so existing scripts keep working. The logic lives in
runner/ticket.py.

Usage:
    draft_ticket.py --title '...' --problem '...' --description '...' --scope '...'
    draft_ticket.py --from-todo <id> --title '...' --problem '...' --scope '...'
"""

import argparse
import sys

from .ticket import add_ticket_content_args, cmd_draft


def main():
    parser = argparse.ArgumentParser(description="Create a backlog draft ticket")
    add_ticket_content_args(parser, explore_hints=False)
    args = parser.parse_args()
    sys.exit(cmd_draft(args, parser))


if __name__ == "__main__":
    main()
