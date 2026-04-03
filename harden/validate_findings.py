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
import re
import sys
from pathlib import Path

REQUIRED_FIELDS = ["category", "description", "severity", "location"]
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_CATEGORIES = {"general", "security", "input", "logic", "complexity", "resilience"}

MAX_STDIN_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_FINDINGS = 1000  # ATK-010: cap findings count to prevent O(n) DoS


def _resolve_filepath(location: str) -> str:
    """
    Strip the line-range suffix from a location string and return the file path.

    A line-range suffix is a trailing colon followed by digits or digit ranges
    (e.g. ":45", ":45-50"). Any colon that is not followed purely by digits and
    hyphens is considered part of the path itself.

    Handles correctly:
      - "path/to/file.py"            -> "path/to/file.py"
      - "path/to/file.py:45-50"      -> "path/to/file.py"
      - "D:/path/to/file.py"         -> "D:/path/to/file.py"
      - "D:/path/to/file.py:45-50"   -> "D:/path/to/file.py"
      - "//server/share/file.py"     -> "//server/share/file.py"
      - "//server/share/file.py:10"  -> "//server/share/file.py"
    """
    parts = location.rsplit(":", 1)
    if len(parts) == 1:
        # No colon at all — bare path
        return location
    candidate_path, suffix = parts[0], parts[1]
    # ATK-004: only treat the colon as a line-range separator when the suffix
    # is a single non-negative integer ("45") or a valid range of two
    # non-negative integers ("45-50"). Patterns like ":-5", ":5-", ":--" are
    # rejected and the colon is treated as part of the path instead.
    if re.fullmatch(r"\d+(?:-\d+)?", suffix):
        return candidate_path
    return location


def validate(findings: list, check_files: bool = False, deep: bool = False) -> list[str]:
    """Validate findings and return a list of error strings (empty = valid)."""
    errors = []

    if not isinstance(findings, list):
        return ["Expected a JSON array of findings"]

    if len(findings) == 0:
        return []  # Empty findings is valid (no issues found)

    # ATK-010: cap findings count to prevent O(n) DoS via huge arrays
    if len(findings) > MAX_FINDINGS:
        return [f"Too many findings: {len(findings)} (maximum {MAX_FINDINGS})"]

    for i, finding in enumerate(findings):
        # ATK-002: type guard BEFORE any attribute access
        if not isinstance(finding, dict):
            errors.append(f"item[{i}]: not a JSON object")
            continue

        # ATK-005: always use a positional label; never use the forbidden id value
        label = f"item[{i}]"

        # findings must NOT include an 'id' field
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

        # ATK-010: enum checks — only run when the value is a non-empty string.
        severity = finding.get("severity")
        if isinstance(severity, str) and severity not in VALID_SEVERITIES:
            errors.append(f"{label}: invalid severity '{severity}' (expected: {', '.join(sorted(VALID_SEVERITIES))})")

        category = finding.get("category")
        if isinstance(category, str) and category not in VALID_CATEGORIES:
            errors.append(f"{label}: invalid category '{category}' (expected: {', '.join(sorted(VALID_CATEGORIES))})")

        # File existence check (code mode)
        if check_files:
            location = finding.get("location", "")
            if location:
                # ATK-085: reject comma-separated multi-file locations
                if "," in location:
                    errors.append(f"{label}: 'location' must reference a single file, got multi-file: {location}")
                else:
                    filepath = _resolve_filepath(location)
                    # ATK-001: use relative_to() to prevent prefix collision attacks
                    resolved = Path(filepath).resolve()
                    cwd = Path.cwd().resolve()
                    try:
                        resolved.relative_to(cwd)
                    except ValueError:
                        errors.append(f"{label}: location escapes project directory: {filepath}")
                        continue
                    if not resolved.exists():
                        errors.append(f"{label}: file not found: {filepath}")

        # Deep mode: require Given/When/Then scenario
        if deep:
            scenario = finding.get("scenario")
            # Check type first so non-string falsy values (0, False, [])
            # always get the type-error message rather than the missing-field message.
            if scenario is None:
                errors.append(f"{label}: --deep requires a 'scenario' field")
            elif not isinstance(scenario, str):
                # Covers non-string falsy (0, False, []) and non-string truthy (dicts)
                errors.append(f"{label}: 'scenario' must be a string, got {type(scenario).__name__}")
            elif not scenario.strip():
                errors.append(f"{label}: --deep requires a 'scenario' field")
            else:
                lower = scenario.lower()
                for keyword in ("given", "when", "then"):
                    if keyword not in lower:
                        errors.append(f"{label}: scenario missing '{keyword}' (expected Given/When/Then format)")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate harden Attacker findings")
    parser.add_argument("--check-files", action="store_true",
                        help="Verify that referenced files exist")
    parser.add_argument("--deep", action="store_true",
                        help="Require Given/When/Then scenarios in each finding")
    args = parser.parse_args()

    # ATK-008: check size BEFORE stripping so whitespace-padded inputs cannot
    # bypass the limit by relying on strip() reducing the byte count
    raw_unstripped = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(raw_unstripped) > MAX_STDIN_BYTES:
        print(f"ERROR: Input exceeds maximum allowed size ({MAX_STDIN_BYTES} bytes)", file=sys.stderr)
        sys.exit(1)
    raw = raw_unstripped.strip()
    if not raw:
        print("ERROR: Empty input", file=sys.stderr)
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

    # ATK-014: echo back the original input verbatim to preserve byte-identity
    print(raw)


if __name__ == "__main__":
    main()
