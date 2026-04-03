#!/usr/bin/env python3
"""
Validate Defender output from the harden skill.

Reads a JSON object from stdin with "responses" and optional "todos" arrays.
Validates that every expected ATK-ID is addressed, actions are valid enums,
no fields are empty, and referenced files exist (optionally).

Exit 0 + prints validated JSON on success.
Exit 1 + prints error details on failure.

Usage:
    echo '<json>' | validate_response.py --expected-ids ATK-001,ATK-002 [--check-files]
"""
import argparse
import json
import sys
from pathlib import Path

REQUIRED_FIELDS = ["finding_ref", "action", "description"]
VALID_ACTIONS = {"fixed", "refactored", "deferred"}

MAX_STDIN_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_RESPONSES = 1000  # ATK-008: cap responses count to prevent O(n) DoS
MAX_EXPECTED_IDS = 500  # ATK-003: cap --expected-ids to bound error output size


def validate(data: dict, expected_ids: set, check_files: bool = False) -> list[str]:
    """Validate defender response and return a list of error strings (empty = valid)."""
    errors = []

    if not isinstance(data, dict):
        return ["Expected a JSON object with 'responses' array"]

    responses = data.get("responses")
    if responses is None:
        return ["Missing 'responses' field"]
    if not isinstance(responses, list):
        return ["'responses' must be a JSON array"]

    # ATK-008: cap responses count to prevent O(n) DoS via huge arrays
    if len(responses) > MAX_RESPONSES:
        return [f"Too many responses: {len(responses)} (maximum {MAX_RESPONSES})"]

    addressed_ids = set()

    for i, resp in enumerate(responses):
        # ATK-003: type guard BEFORE any attribute access
        if not isinstance(resp, dict):
            errors.append(f"response[{i}]: not a JSON object")
            continue

        # ATK-003: only use finding_ref as label after confirming it is a string;
        # a non-string finding_ref (e.g. integer) would produce a confusing label.
        raw_ref = resp.get("finding_ref")
        label = raw_ref if isinstance(raw_ref, str) and raw_ref.strip() else f"response[{i}]"

        # Required fields present, string-typed, and non-empty.
        # Track which fields already have errors to avoid duplicate messages.
        field_errors: set[str] = set()
        for field in REQUIRED_FIELDS:
            val = resp.get(field)
            if val is None:
                errors.append(f"{label}: missing required field '{field}'")
                field_errors.add(field)
            elif not isinstance(val, str):
                # ATK-007: non-string values are invalid
                errors.append(f"{label}: field '{field}' must be a string, got {type(val).__name__}")
                field_errors.add(field)
            elif not val.strip():
                errors.append(f"{label}: field '{field}' is empty")
                field_errors.add(field)

        # Valid action enum — ATK-004: validate unconditionally so this check remains
        # correct even if 'action' is removed from REQUIRED_FIELDS in the future.
        action = resp.get("action")
        if "action" not in field_errors:
            # Only emit enum error if REQUIRED_FIELDS didn't already flag this field
            if isinstance(action, str) and action.strip():
                if action not in VALID_ACTIONS:
                    errors.append(f"{label}: invalid action '{action}' (expected: {', '.join(sorted(VALID_ACTIONS))})")
            else:
                errors.append(f"{label}: field 'action' must be one of: {', '.join(sorted(VALID_ACTIONS))}")

        # ATK-013: detect and reject duplicate finding_refs
        finding_ref = resp.get("finding_ref")
        if isinstance(finding_ref, str):
            if finding_ref in addressed_ids:
                errors.append(f"{label}: duplicate finding_ref '{finding_ref}' — each ATK-ID must be addressed exactly once")
            else:
                addressed_ids.add(finding_ref)

        # Structural validation of changes array — always enforced (ATK-006)
        changes = resp.get("changes")
        if changes is not None and not isinstance(changes, list):
            errors.append(f"{label}: 'changes' must be a JSON array, got {type(changes).__name__}")
        elif isinstance(changes, list):
            for j, change in enumerate(changes):
                if not isinstance(change, dict):
                    errors.append(f"{label}: changes[{j}]: not a JSON object")

        # File existence check (code mode) — gated on --check-files flag
        if check_files and isinstance(changes, list):
            for j, change in enumerate(changes):
                if not isinstance(change, dict):
                    continue  # already reported above
                filepath = change.get("file", "")
                if filepath:
                    # ATK-002: use relative_to() to prevent prefix collision attacks
                    resolved = Path(filepath).resolve()
                    cwd = Path.cwd().resolve()
                    try:
                        resolved.relative_to(cwd)
                    except ValueError:
                        errors.append(f"{label}: file escapes project directory: {filepath}")
                        continue
                    if not resolved.exists():
                        errors.append(f"{label}: file not found: {filepath}")

    # Report missing IDs and show which refs were actually found
    missing = expected_ids - addressed_ids
    if missing:
        found_refs = sorted(addressed_ids) if addressed_ids else []
        found_summary = (
            f" (found refs: {', '.join(found_refs)})" if found_refs else " (no finding_refs found in responses)"
        )
        for mid in sorted(missing):
            errors.append(f"Finding {mid} not addressed in any response{found_summary}")

    # Validate optional todos array
    todos = data.get("todos", [])
    if not isinstance(todos, list):
        errors.append("'todos' must be a JSON array")
    else:
        for i, todo in enumerate(todos):
            if not isinstance(todo, dict):
                errors.append(f"todos[{i}]: not a JSON object")
                continue
            content = todo.get("content", "")
            # ATK-009: reject non-string content
            if not isinstance(content, str):
                errors.append(f"todos[{i}]: 'content' must be a string, got {type(content).__name__}")
            elif not content.strip():
                errors.append(f"todos[{i}]: empty 'content' field")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate harden Defender response")
    parser.add_argument("--expected-ids", required=True,
                        help="Comma-separated list of ATK-IDs that must be addressed")
    parser.add_argument("--check-files", action="store_true",
                        help="Verify that referenced files exist")
    args = parser.parse_args()

    # ATK-003: require at least one valid ID
    expected = {id_.strip() for id_ in args.expected_ids.split(",") if id_.strip()}
    if not expected:
        print("ERROR: --expected-ids must contain at least one non-empty ATK-ID", file=sys.stderr)
        sys.exit(1)
    if len(expected) > MAX_EXPECTED_IDS:
        print(f"ERROR: --expected-ids exceeds maximum allowed count ({MAX_EXPECTED_IDS})", file=sys.stderr)
        sys.exit(1)

    # ATK-009: check size BEFORE stripping
    raw_unstripped = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(raw_unstripped) > MAX_STDIN_BYTES:
        print(f"ERROR: Input exceeds maximum allowed size ({MAX_STDIN_BYTES} bytes)", file=sys.stderr)
        sys.exit(1)
    raw = raw_unstripped.strip()
    if not raw:
        print("ERROR: Empty input", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate(data, expected_ids=expected, check_files=args.check_files)

    if errors:
        print(f"VALIDATION FAILED ({len(errors)} errors):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    # Echo back the original input verbatim to preserve byte-identity
    print(raw)


if __name__ == "__main__":
    main()
