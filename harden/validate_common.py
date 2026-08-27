#!/usr/bin/env python3
"""
Shared utilities for harden validators and the validate-and-assign script.

Provides JSON input reading (stdin or file), path safety checks,
and error reporting.
"""

import json
import sys
from pathlib import Path

MAX_STDIN_BYTES = 10 * 1024 * 1024  # 10 MB


def read_json_input(file_path: str = None, empty_ok: bool = False) -> tuple[str, any]:
    """Read and parse JSON from a file or stdin.

    Returns (raw_text, parsed_json). Exits with error on failure.
    If empty_ok is True and input is empty, returns ("", None).
    """
    if file_path:
        path = Path(file_path)
        if not path.exists():
            print(f"ERROR: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"ERROR: Cannot read file: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        raw_unstripped = sys.stdin.read(MAX_STDIN_BYTES + 1)
        if len(raw_unstripped) > MAX_STDIN_BYTES:
            print(
                f"ERROR: Input exceeds maximum allowed size ({MAX_STDIN_BYTES} bytes)",
                file=sys.stderr,
            )
            sys.exit(1)
        raw = raw_unstripped.strip()

    if not raw:
        if empty_ok:
            return "", None
        print("ERROR: Empty input", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    return raw, data


def check_path_escape(filepath: str) -> str | None:
    """Check if a file path escapes the project directory.

    Returns an error string if the path escapes, None if safe.
    """
    resolved = Path(filepath).resolve()
    cwd = Path.cwd().resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError:
        return f"file escapes project directory: {filepath}"
    return None


def report_errors(errors: list[str]) -> None:
    """Print validation errors to stderr and exit 1. No-op if errors is empty."""
    if not errors:
        return
    print(f"VALIDATION FAILED ({len(errors)} errors):", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    sys.exit(1)
