#!/usr/bin/env python3
"""
Validate Attacker output from the harden skill.

Reads a JSON array of findings from stdin and validates required fields,
valid enums, non-empty values, optional file existence, and optional
Given/When/Then scenarios.

Exit 0 + prints validated JSON on success.
Exit 1 + prints error details on failure.

Usage:
    echo '<json>' | validate_findings.py [--check-files] [--deep]
"""
import argparse
import json
import os
import sys

REQUIRED_FIELDS = ["category", "description", "severity", "location"]
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_CATEGORIES = {"general", "security", "input", "logic", "complexity", "resilience"}

MAX_STDIN_BYTES = 10 * 1024 * 1024  # 10 MB


def _resolve_filepath(location: str) -> str:
    """
    Strip the line-range suffix from a location string and return the file path.

    Handles Windows drive letters correctly. A location may be:
      - "path/to/file.py"            -> "path/to/file.py"
      - "path/to/file.py:45-50"      -> "path/to/file.py"
      - "D:/path/to/file.py"         -> "D:/path/to/file.py"
      - "D:/path/to/file.py:45-50"   -> "D:/path/to/file.py"
    """
    parts = location.rsplit(":", 1)
    if len(parts) == 1:
        # No colon at all — bare path
        return location
    candidate_path, suffix = parts[0], parts[1]
    # If candidate_path is a single letter, rsplit consumed a Windows drive
    # letter (e.g. "D"), not a line-range separator. Return the full string.
    if len(candidate_path) == 1 and candidate_path.isalpha():
        return location
    # suffix should look like digits / a line range; if it's not, the colon
    # is part of the path (rare, but be safe) — still prefer stripping it
    # because line ranges are always numeric.
    return candidate_path


def validate(findings: list, check_files: bool = False, deep: bool = False) -> list[str]:
    """Validate findings and return a list of error strings (empty = valid)."""
    errors = []

    if not isinstance(findings, list):
        return ["Expected a JSON array of findings"]

    if len(findings) == 0:
        return []  # Empty findings is valid (no issues found)

    for i, finding in enumerate(findings):
        # ATK-002: type guard BEFORE any attribute access
        if not isinstance(finding, dict):
            errors.append(f"item[{i}]: not a JSON object")
            continue

        label = finding.get("id", f"item[{i}]")

        # ATK-012: findings must NOT include an 'id' field
        if "id" in finding:
            errors.append(f"{label}: findings must not include an 'id' field (assigned by post-processor)")

        # Required fields present and non-empty
        for field in REQUIRED_FIELDS:
            val = finding.get(field)
            if val is None:
                errors.append(f"{label}: missing required field '{field}'")
            elif not isinstance(val, str):
                # ATK-007: non-string values for required fields are invalid
                errors.append(f"{label}: field '{field}' must be a string, got {type(val).__name__}")
            elif not val.strip():
                errors.append(f"{label}: field '{field}' is empty")

        # Valid severity enum — ATK-008: always check, even for empty string
        severity = finding.get("severity")
        if severity is not None:
            if not isinstance(severity, str) or not severity.strip():
                errors.append(f"{label}: field 'severity' must be a non-empty string")
            elif severity not in VALID_SEVERITIES:
                errors.append(f"{label}: invalid severity '{severity}' (expected: {', '.join(sorted(VALID_SEVERITIES))})")

        # Valid category enum — ATK-008: always check, even for empty string
        category = finding.get("category")
        if category is not None:
            if not isinstance(category, str) or not category.strip():
                errors.append(f"{label}: field 'category' must be a non-empty string")
            elif category not in VALID_CATEGORIES:
                errors.append(f"{label}: invalid category '{category}' (expected: {', '.join(sorted(VALID_CATEGORIES))})")

        # File existence check (code mode) — ATK-001: handle bare Windows paths
        if check_files:
            location = finding.get("location", "")
            if location:
                filepath = _resolve_filepath(location)
                if filepath and not os.path.exists(filepath):
                    errors.append(f"{label}: file not found: {filepath}")

        # Deep mode: require Given/When/Then scenario — ATK-010: check all keywords
        if deep:
            scenario = finding.get("scenario", "")
            if not scenario:
                errors.append(f"{label}: --deep requires a 'scenario' field")
            elif isinstance(scenario, str):
                lower = scenario.lower()
                for keyword in ("given", "when", "then"):
                    if keyword not in lower:
                        errors.append(f"{label}: scenario missing '{keyword}' (expected Given/When/Then format)")
                # No break — all three keywords are checked independently

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate harden Attacker findings")
    parser.add_argument("--check-files", action="store_true",
                        help="Verify that referenced files exist")
    parser.add_argument("--deep", action="store_true",
                        help="Require Given/When/Then scenarios in each finding")
    args = parser.parse_args()

    # ATK-005: limit stdin read size to avoid memory exhaustion
    raw = sys.stdin.read(MAX_STDIN_BYTES + 1).strip()
    if not raw:
        print("ERROR: Empty input", file=sys.stderr)
        sys.exit(1)
    if len(raw) > MAX_STDIN_BYTES:
        print(f"ERROR: Input exceeds maximum allowed size ({MAX_STDIN_BYTES} bytes)", file=sys.stderr)
        sys.exit(1)

    try:
        findings = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate(findings, check_files=args.check_files, deep=args.deep)

    if errors:
        print(f"VALIDATION FAILED ({len(errors)} errors):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    # Valid — echo back the JSON
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
