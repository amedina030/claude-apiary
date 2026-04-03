#!/usr/bin/env python3
"""
Assign deterministic sequential IDs to harden agent output.

Reads a JSON array from stdin, adds an "id" field to each object,
and writes the result to stdout.

Usage:
    echo '[{"category": "security", ...}]' | assign_ids.py --prefix ATK
    echo '[{"finding_ref": "ATK-001", ...}]' | assign_ids.py --prefix DEF
"""
import argparse
import json
import sys


def assign_ids(items: list, prefix: str) -> list:
    """Add sequential IDs (PREFIX-001, PREFIX-002, ...) to each item."""
    for i, item in enumerate(items, start=1):
        item["id"] = f"{prefix}-{i:03d}"
    return items


def main():
    parser = argparse.ArgumentParser(description="Assign sequential IDs to harden output")
    parser.add_argument("--prefix", required=True, choices=["ATK", "DEF"],
                        help="ID prefix (ATK for findings, DEF for responses)")
    args = parser.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        print("[]")
        return

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(items, list):
        print("ERROR: Expected a JSON array", file=sys.stderr)
        sys.exit(1)

    result = assign_ids(items, args.prefix)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
