#!/usr/bin/env python3
"""
Validate Attacker output from the harden skill.

Reads a JSON array of findings from stdin (or --file) and validates required
fields, valid enums, non-empty values, optional file existence, and optional
Given/When/Then scenarios.

Exit 0 + prints validated JSON on success.
Exit 1 + prints error details on failure.

Usage:
    echo '<json>' | validate_findings.py [--check-files] [--deep]
    validate_findings.py --file findings.json [--check-files] [--deep]
"""

import argparse
import json
import re
from pathlib import Path

from lenses import LENSES, is_valid_lens
from validate_common import check_path_escape, read_json_input, report_errors

REQUIRED_FIELDS = ["category", "description", "severity", "location"]
# In lens mode the per-agent lens replaces the free-form category, so category
# is neither required nor validated against the legacy enum.
LENS_REQUIRED_FIELDS = ["description", "severity", "location"]
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_CATEGORIES = {"general", "security", "input", "logic", "complexity", "resilience"}

MAX_FINDINGS = 1000  # ATK-010: cap findings count to prevent O(n) DoS

# Map common invalid categories to valid ones
CATEGORY_MAP = {
    "correctness": "logic",
    "robustness": "resilience",
    "performance": "complexity",
    "error-handling": "resilience",
    "error_handling": "resilience",
    "validation": "input",
    "data-validation": "input",
    "maintainability": "complexity",
}

# Fields the Attacker is allowed to produce (plus "scenario" in deep mode)
ALLOWED_FIELDS = {"category", "description", "severity", "location", "scenario"}


def sanitize(findings: list, deep: bool = False, lens: str = None) -> list:
    """Auto-fix common Attacker output issues: strip unknown fields, map invalid categories.

    In lens mode (``lens`` set), each finding is reduced to the lens-mode fields
    and stamped with its ``lens`` so the merged multi-lens set is self-describing
    for the consolidator. The legacy category-mapping path is skipped.
    """
    if not isinstance(findings, list):
        return findings

    if lens is not None:
        allowed = {"severity", "description", "location", "lens"}
        if deep:
            allowed.add("scenario")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            for key in list(finding.keys()):
                if key not in allowed:
                    del finding[key]
            finding["lens"] = lens.lower()
        return findings

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        # Strip unknown fields
        allowed = ALLOWED_FIELDS if deep else ALLOWED_FIELDS - {"scenario"}
        for key in list(finding.keys()):
            if key not in allowed:
                del finding[key]
        # Map invalid categories
        cat = finding.get("category", "")
        if isinstance(cat, str) and cat.lower() in CATEGORY_MAP:
            finding["category"] = CATEGORY_MAP[cat.lower()]
    return findings


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


def validate(
    findings: list, check_files: bool = False, deep: bool = False, lens: str = None
) -> list[str]:
    """Validate findings and return a list of error strings (empty = valid).

    When ``lens`` is set, validates lens-mode findings: the per-agent lens
    replaces ``category`` (no category enum check), and any ``lens`` field
    carried on a finding must match the attacker's assigned lens.
    """
    errors = []

    if lens is not None and not is_valid_lens(lens):
        return [f"invalid lens '{lens}' (expected one of: {', '.join(sorted(LENSES))})"]

    if not isinstance(findings, list):
        return ["Expected a JSON array of findings"]

    if len(findings) == 0:
        return []  # Empty findings is valid (no issues found)

    # ATK-010: cap findings count to prevent O(n) DoS via huge arrays
    if len(findings) > MAX_FINDINGS:
        return [f"Too many findings: {len(findings)} (maximum {MAX_FINDINGS})"]

    required = LENS_REQUIRED_FIELDS if lens is not None else REQUIRED_FIELDS

    for i, finding in enumerate(findings):
        # ATK-002: type guard BEFORE any attribute access
        if not isinstance(finding, dict):
            errors.append(f"item[{i}]: not a JSON object")
            continue

        # ATK-005: always use a positional label; never use the forbidden id value
        label = f"item[{i}]"

        # findings must NOT include an 'id' field
        if "id" in finding:
            errors.append(
                f"{label}: findings must not include an 'id' field (assigned by post-processor)"
            )

        # Required fields present and non-empty
        for field in required:
            val = finding.get(field)
            if val is None:
                errors.append(f"{label}: missing required field '{field}'")
            elif not isinstance(val, str):
                # ATK-007: non-string values for required fields are invalid
                errors.append(
                    f"{label}: field '{field}' must be a string, got {type(val).__name__}"
                )
            elif not val.strip():
                errors.append(f"{label}: field '{field}' is empty")

        # ATK-010: enum checks — only run when the value is a non-empty string.
        severity = finding.get("severity")
        if isinstance(severity, str) and severity not in VALID_SEVERITIES:
            errors.append(
                f"{label}: invalid severity '{severity}' (expected: {', '.join(sorted(VALID_SEVERITIES))})"
            )

        if lens is None:
            category = finding.get("category")
            if isinstance(category, str) and category not in VALID_CATEGORIES:
                errors.append(
                    f"{label}: invalid category '{category}' (expected: {', '.join(sorted(VALID_CATEGORIES))})"
                )
        else:
            # Lens mode: a carried 'lens' must match the attacker's assigned lens.
            fl = finding.get("lens")
            if fl is not None and (not isinstance(fl, str) or fl.lower() != lens.lower()):
                errors.append(f"{label}: lens '{fl}' does not match attacker lens '{lens}'")

        # File existence check (code mode)
        if check_files:
            location = finding.get("location", "")
            if location:
                # ATK-085: reject comma-separated multi-file locations
                if "," in location:
                    errors.append(
                        f"{label}: 'location' must reference a single file, got multi-file: {location}"
                    )
                else:
                    filepath = _resolve_filepath(location)
                    escape_err = check_path_escape(filepath)
                    if escape_err:
                        errors.append(f"{label}: location {escape_err}")
                        continue
                    if not Path(filepath).resolve().exists():
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
                errors.append(
                    f"{label}: 'scenario' must be a string, got {type(scenario).__name__}"
                )
            elif not scenario.strip():
                errors.append(f"{label}: --deep requires a 'scenario' field")
            else:
                lower = scenario.lower()
                for keyword in ("given", "when", "then"):
                    if keyword not in lower:
                        errors.append(
                            f"{label}: scenario missing '{keyword}' (expected Given/When/Then format)"
                        )

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate harden Attacker findings")
    parser.add_argument(
        "--check-files", action="store_true", help="Verify that referenced files exist"
    )
    parser.add_argument(
        "--deep", action="store_true", help="Require Given/When/Then scenarios in each finding"
    )
    parser.add_argument("--file", dest="file_path", help="Read JSON from file instead of stdin")
    parser.add_argument(
        "--sanitize",
        action="store_true",
        help="Auto-fix common issues (strip unknown fields, map invalid categories)",
    )
    parser.add_argument(
        "--lens",
        help="Validate as lens-mode findings for the given lens "
        "(replaces the legacy category field)",
    )
    args = parser.parse_args()

    raw, findings = read_json_input(file_path=args.file_path)
    if args.sanitize:
        findings = sanitize(findings, deep=args.deep, lens=args.lens)
    errors = validate(findings, check_files=args.check_files, deep=args.deep, lens=args.lens)
    report_errors(errors)

    # When sanitized, output the cleaned version; otherwise echo original
    if args.sanitize:
        print(json.dumps(findings, indent=2))
    else:
        print(raw)


if __name__ == "__main__":
    main()
