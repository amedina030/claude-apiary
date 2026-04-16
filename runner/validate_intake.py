#!/usr/bin/env python3
"""
Validate an intake JSON file for the autonomous runner.

Checks required fields, types, minimum content thresholds, and ISO date format.

Exit 0 + prints "Valid" on success.
Exit 1 + prints error details on failure.

Usage:
    validate_intake.py <path_to_intake.json>
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


REQUIRED_FIELDS = ["id", "title", "problem", "description", "scope", "created_at"]
MIN_LENGTH_FIELDS = {"problem": 20, "description": 20}


def validate(data: dict) -> list[str]:
    """Validate intake data and return a list of error strings (empty = valid)."""
    errors = []

    if not isinstance(data, dict):
        return ["Expected a JSON object"]

    # Required fields present and non-empty strings
    for field in REQUIRED_FIELDS:
        val = data.get(field)
        if val is None:
            errors.append(f"missing required field '{field}'")
        elif not isinstance(val, str):
            errors.append(f"field '{field}' must be a string, got {type(val).__name__}")
        elif not val.strip():
            errors.append(f"field '{field}' is empty")

    # Minimum length checks
    for field, min_len in MIN_LENGTH_FIELDS.items():
        val = data.get(field)
        if isinstance(val, str) and val.strip() and len(val.strip()) < min_len:
            errors.append(f"{field} is too short (minimum {min_len} characters)")

    # Optional explore_hints: list of repo-relative path strings. Refiner uses
    # these as a starting set instead of exploring the codebase from zero.
    hints = data.get("explore_hints")
    if hints is not None:
        if not isinstance(hints, list):
            errors.append(f"explore_hints must be a list, got {type(hints).__name__}")
        else:
            for i, h in enumerate(hints):
                if not isinstance(h, str) or not h.strip():
                    errors.append(f"explore_hints[{i}] must be a non-empty string")

    # ISO date format check for created_at
    created_at = data.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        try:
            datetime.fromisoformat(created_at)
        except ValueError:
            errors.append(f"created_at is not valid ISO format: '{created_at}'")

    # Optional target_repo: non-empty string pointing at an existing directory
    # with a .git entry. Enables phase 3 multi-repo runs; absent field keeps
    # pre-phase-3 behavior (runs against apiary).
    target_repo = data.get("target_repo")
    if target_repo is not None:
        if not isinstance(target_repo, str):
            errors.append(
                f"target_repo must be a string, got {type(target_repo).__name__}"
            )
        elif not target_repo.strip():
            errors.append("target_repo is empty")
        else:
            p = Path(target_repo.strip())
            if not p.exists():
                errors.append(f"target_repo path does not exist: {p}")
            elif not p.is_dir():
                errors.append(f"target_repo path is not a directory: {p}")
            elif not (p / ".git").exists():
                errors.append(
                    f"target_repo path is not a git repository (no .git entry): {p}"
                )

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate runner intake file")
    parser.add_argument("file", help="Path to intake JSON file")
    args = parser.parse_args()

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate(data)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    print("Valid")


if __name__ == "__main__":
    main()
