#!/usr/bin/env python3
"""
Assign deterministic sequential IDs to harden agent output.

Reads a JSON array from stdin (or --file), adds an "id" field to each object,
and writes the result to stdout.

Usage:
    echo '[{"category": "security", ...}]' | assign_ids.py --prefix ATK
    assign_ids.py --prefix ATK --file findings.json
"""

import argparse
import json
import re
import sys

from validate_common import read_json_input

# Accepted ID prefixes: ATK (legacy findings), ATK-<CODE> (per-lens findings,
# e.g. ATK-SEC), CON (consolidated findings), DEF (defender responses). The
# <CODE> group matches the 3-letter lens codes from lenses.py without importing
# it here — assign_ids stays a dumb, lens-agnostic ID stamper.
PREFIX_RE = re.compile(r"^(ATK(-[A-Z]{2,4})?|CON|DEF)$")


def _prefix(value: str) -> str:
    if not PREFIX_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"invalid prefix {value!r} (expected ATK, ATK-<CODE>, CON, or DEF)"
        )
    return value


def assign_ids(items: list, prefix: str) -> list:
    """Add sequential IDs (PREFIX-001, PREFIX-002, ...) to each item."""
    for i, item in enumerate(items, start=1):
        item["id"] = f"{prefix}-{i:03d}"
    return items


def main():
    parser = argparse.ArgumentParser(description="Assign sequential IDs to harden output")
    parser.add_argument(
        "--prefix",
        required=True,
        type=_prefix,
        help="ID prefix: ATK, ATK-<CODE> (per-lens), CON, or DEF",
    )
    parser.add_argument("--file", dest="file_path", help="Read JSON from file instead of stdin")
    args = parser.parse_args()

    _raw, items = read_json_input(file_path=args.file_path, empty_ok=True)

    if items is None:
        print("[]")
        return

    if not isinstance(items, list):
        print("ERROR: Expected a JSON array", file=sys.stderr)
        sys.exit(1)

    result = assign_ids(items, args.prefix)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
