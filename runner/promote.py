#!/usr/bin/env python3
"""Promote a backlog ticket to runner intake.

Validates, assigns a UUID, copies to intake/, and removes the backlog file.

DEPRECATED ENTRY POINT — a thin shim over ``python -m runner.ticket promote``,
kept for one release so existing scripts keep working. The logic lives in
runner/ticket.py.

Usage:
    promote.py <slug>
"""
import argparse
import sys

from .ticket import add_promote_args, cmd_promote


def main():
    parser = argparse.ArgumentParser(
        description="Promote a backlog ticket to runner intake.",
    )
    add_promote_args(parser)
    args = parser.parse_args()
    sys.exit(cmd_promote(args, parser))


if __name__ == '__main__':
    main()
