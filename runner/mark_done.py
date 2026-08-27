#!/usr/bin/env python3
"""Mark a backlog ticket as done without running it through the runner.

For tickets small enough to fix by hand. Deletes runner/backlog/<slug>.json.
The presence of the backlog file is itself the safety check: promote.py
removes the backlog file when a ticket enters intake, so a backlog file
that still exists is guaranteed not to be in flight.

Usage:
    mark_done.py <slug> [--note "explanation"]
"""

import argparse
import sys
from pathlib import Path

from runner.target_repo import backlog_dir

SCRIPT_DIR = Path(__file__).resolve().parent
BACKLOG_DIR = backlog_dir()


def _slug_is_safe(slug: str) -> bool:
    if (
        "/" in slug
        or "\\" in slug
        or "\x00" in slug
        or slug in (".", "..")
        or Path(slug) != Path(Path(slug).name)
        or not Path(slug).name
    ):
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Mark a backlog ticket as done.")
    parser.add_argument("slug", help="Backlog ticket slug (filename without .json)")
    parser.add_argument("--note", default="", help="Optional note describing the manual completion")
    args = parser.parse_args()

    slug = args.slug
    if not _slug_is_safe(slug):
        print("Error: invalid slug (path separators not allowed)", file=sys.stderr)
        sys.exit(1)

    backlog_path = BACKLOG_DIR / f"{slug}.json"
    if not backlog_path.exists():
        print(f"Error: backlog ticket {slug}.json not found", file=sys.stderr)
        sys.exit(1)

    backlog_path.unlink()
    print(f"Marked {slug} as done.")


if __name__ == "__main__":
    main()
