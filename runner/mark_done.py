#!/usr/bin/env python3
"""Mark a backlog ticket as done without running it through the runner.

DEPRECATED ENTRY POINT — a thin shim over ``python -m runner.ticket mark-done``,
kept for one release so existing scripts keep working. The logic lives in
runner/ticket.py.

Usage:
    mark_done.py <slug> [--note "explanation"]
"""

import argparse
import sys

from .ticket import add_mark_done_args, cmd_mark_done


def main():
    parser = argparse.ArgumentParser(description="Mark a backlog ticket as done.")
    add_mark_done_args(parser)
    args = parser.parse_args()
    sys.exit(cmd_mark_done(args, parser))


if __name__ == "__main__":
    main()
