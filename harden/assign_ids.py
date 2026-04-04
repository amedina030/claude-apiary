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
import sys

from validate_common import read_json_input


def assign_ids(items: list, prefix: str) -> list:
    """Add sequential IDs (PREFIX-001, PREFIX-002, ...) to each item."""
    for i, item in enumerate(items, start=1):
        item["id"] = f"{prefix}-{i:03d}"
    return items


def main():
    parser = argparse.ArgumentParser(description="Assign sequential IDs to harden output")
    parser.add_argument("--prefix", required=True, choices=["ATK", "DEF"],
                        help="ID prefix (ATK for findings, DEF for responses)")
    parser.add_argument("--file", dest="file_path",
                        help="Read JSON from file instead of stdin")
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
