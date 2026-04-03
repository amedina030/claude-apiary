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
import os
import sys

REQUIRED_FIELDS = ["finding_ref", "action", "description"]
VALID_ACTIONS = {"fixed", "refactored", "deferred"}

MAX_STDIN_BYTES = 10 * 1024 * 1024  # 10 MB


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

    addressed_ids = set()

    for i, resp in enumerate(responses):
        # ATK-003: type guard BEFORE any attribute access
        if not isinstance(resp, dict):
            errors.append(f"response[{i}]: not a JSON object")
            continue

        label = resp.get("finding_ref", f"response[{i}]")

        # Required fields present and non-empty
        for field in REQUIRED_FIELDS:
            val = resp.get(field)
            if val is None:
                errors.append(f"{label}: missing required field '{field}'")
            elif isinstance(val, str) and not val.strip():
                errors.append(f"{label}: field '{field}' is empty")

        # Valid action enum — ATK-009: always check, even for empty string
        action = resp.get("action")
        if action is not None:
            if not isinstance(action, str) or not action.strip():
                errors.append(f"{label}: field 'action' must be a non-empty string")
            elif action not in VALID_ACTIONS:
                errors.append(f"{label}: invalid action '{action}' (expected: {', '.join(sorted(VALID_ACTIONS))})")

        # Track which ATK-IDs are addressed
        finding_ref = resp.get("finding_ref", "")
        if finding_ref:
            addressed_ids.add(finding_ref)

        # File existence check (code mode)
        if check_files:
            changes = resp.get("changes", [])
            if isinstance(changes, list):
                for change in changes:
                    if isinstance(change, dict):
                        filepath = change.get("file", "")
                        if filepath and not os.path.exists(filepath):
                            errors.append(f"{label}: file not found: {filepath}")

    # ATK-011: report missing IDs and show which refs were actually found
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
            if not content or (isinstance(content, str) and not content.strip()):
                errors.append(f"todos[{i}]: empty 'content' field")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate harden Defender response")
    parser.add_argument("--expected-ids", required=True,
                        help="Comma-separated list of ATK-IDs that must be addressed")
    parser.add_argument("--check-files", action="store_true",
                        help="Verify that referenced files exist")
    args = parser.parse_args()

    # ATK-004: filter out empty strings from the IDs list
    expected = {id_.strip() for id_ in args.expected_ids.split(",") if id_.strip()}

    # ATK-006: limit stdin read size to avoid memory exhaustion
    raw = sys.stdin.read(MAX_STDIN_BYTES + 1).strip()
    if not raw:
        print("ERROR: Empty input", file=sys.stderr)
        sys.exit(1)
    if len(raw) > MAX_STDIN_BYTES:
        print(f"ERROR: Input exceeds maximum allowed size ({MAX_STDIN_BYTES} bytes)", file=sys.stderr)
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

    # Valid — echo back the JSON
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
