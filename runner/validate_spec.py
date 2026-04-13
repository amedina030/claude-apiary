#!/usr/bin/env python3
"""
Validate a spec JSON file against the 8 handoff validation rules.

Expected JSON structure:
{
  "goal": {"problem": str, "solution": str, "value": str},
  "shape": {
    "components": [{"name": str, "description": str}, ...],
    "integration_point": str,
    "pattern": str,
    "data_flow": str,
    "dependencies": str
  },
  "behavior": {
    "input": str,
    "processing": str,
    "output": str,
    "error_cases": [{"trigger": str, "behavior": str}, ...],
    "edge_cases": [{"condition": str, "behavior": str}, ...]
  },
  "boundaries": {
    "in_scope": [str, ...],
    "out_of_scope": [{"item": str, "reason": str}, ...],
    "must_not_break": [str, ...]
  },
  "acceptance_criteria": [str, ...]
}

Exit 0 + prints "Valid" on success.
Exit 1 + prints error details on failure.

Usage:
    validate_spec.py <path_to_spec.json>
"""
import argparse
import json
import re
import sys


VAGUE_PATTERNS = re.compile(
    r"\b(works?\s+correctly|handles?\s+gracefully|functions?\s+properly|"
    r"behaves?\s+as\s+expected|performs?\s+well|is\s+robust)\b",
    re.IGNORECASE,
)

PLACEHOLDER_PATTERNS = re.compile(
    # TODO/TBD/FIXME must be ALL-CAPS to match. The scoped (?-i:...) disables
    # IGNORECASE for just this alternation so lowercase domain words like
    # "todo" (scribe note type) don't false-positive. The remaining patterns
    # (placeholder / fill in / to be determined) stay case-insensitive because
    # those are natural-language phrases.
    r"(?-i:(?<![/-])\b(TODO|TBD|FIXME)\b(?![-]|\s*#))"
    r"|\bplaceholder\b"
    r"|(?<!\w)fill\s+in(?=\s+(this|here|later|above|below|the\s+blank))"
    r"|\bto\s+be\s+determined\b",
    re.IGNORECASE,
)

# Sections and their required sub-fields
REQUIRED_STRUCTURE = {
    "goal": ["problem", "solution", "value"],
    "shape": ["components", "integration_point", "pattern", "data_flow"],
    "behavior": ["input", "processing", "output", "error_cases", "edge_cases"],
    "boundaries": ["in_scope", "out_of_scope", "must_not_break"],
    "acceptance_criteria": None,  # top-level list, not a dict
}


def _is_empty(val) -> bool:
    """Check if a value is empty or placeholder-like."""
    if val is None:
        return True
    if isinstance(val, str):
        return not val.strip() or bool(PLACEHOLDER_PATTERNS.search(val))
    if isinstance(val, list):
        return len(val) == 0
    return False


def validate(data: dict) -> list[str]:
    """Validate spec data against the 8 handoff rules. Returns error strings."""
    errors = []

    if not isinstance(data, dict):
        return ["Expected a JSON object"]

    # --- Rule 8: No field left empty or placeholder ---
    for section, subfields in REQUIRED_STRUCTURE.items():
        val = data.get(section)
        if val is None:
            errors.append(f"Rule 8: missing required section '{section}'")
            continue

        if subfields is None:
            # Top-level list (acceptance_criteria)
            if _is_empty(val):
                errors.append(f"Rule 8: '{section}' is empty")
        elif isinstance(val, dict):
            for sf in subfields:
                sv = val.get(sf)
                if _is_empty(sv):
                    errors.append(f"Rule 8: '{section}.{sf}' is empty or placeholder")
        else:
            errors.append(f"Rule 8: '{section}' should be an object, got {type(val).__name__}")

    # If basic structure is missing, remaining rules can't be checked
    if any("missing required section" in e for e in errors):
        return errors

    goal = data.get("goal", {})
    shape = data.get("shape", {})
    behavior = data.get("behavior", {})
    boundaries = data.get("boundaries", {})
    criteria = data.get("acceptance_criteria", [])

    # --- Rule 1: Acceptance criteria must reference specific input/output ---
    if isinstance(criteria, list):
        for i, ac in enumerate(criteria):
            if isinstance(ac, str) and VAGUE_PATTERNS.search(ac):
                errors.append(
                    f"Rule 1: acceptance_criteria[{i}] uses vague language: '{ac[:80]}...'"
                )

    # --- Rule 2: Every error case has a corresponding acceptance criterion ---
    error_cases = behavior.get("error_cases", [])
    if isinstance(error_cases, list) and isinstance(criteria, list):
        criteria_text = " ".join(c for c in criteria if isinstance(c, str)).lower()
        for i, ec in enumerate(error_cases):
            if isinstance(ec, dict):
                trigger = ec.get("trigger", "")
                # Check if any criterion references this error case
                if isinstance(trigger, str) and trigger.strip():
                    # Look for key words from the trigger in criteria
                    keywords = [w for w in trigger.lower().split() if len(w) > 3]
                    if keywords and not any(kw in criteria_text for kw in keywords):
                        errors.append(
                            f"Rule 2: error_cases[{i}] (trigger: '{trigger[:60]}') "
                            f"has no matching acceptance criterion"
                        )

    # --- Rule 3: Every edge case has a corresponding acceptance criterion ---
    edge_cases = behavior.get("edge_cases", [])
    if isinstance(edge_cases, list) and isinstance(criteria, list):
        criteria_text = " ".join(c for c in criteria if isinstance(c, str)).lower()
        for i, ec in enumerate(edge_cases):
            if isinstance(ec, dict):
                condition = ec.get("condition", "")
                if isinstance(condition, str) and condition.strip():
                    keywords = [w for w in condition.lower().split() if len(w) > 3]
                    if keywords and not any(kw in criteria_text for kw in keywords):
                        errors.append(
                            f"Rule 3: edge_cases[{i}] (condition: '{condition[:60]}') "
                            f"has no matching acceptance criterion"
                        )

    # --- Rule 4: Components lists at least one component with description ---
    components = shape.get("components", [])
    if not isinstance(components, list) or len(components) == 0:
        errors.append("Rule 4: shape.components must list at least one component")
    elif isinstance(components, list):
        valid_components = [
            c for c in components
            if isinstance(c, dict) and c.get("name") and c.get("description")
        ]
        if not valid_components:
            errors.append(
                "Rule 4: shape.components has no component with both 'name' and 'description'"
            )

    # --- Rule 5: Data flow contains at least one arrow (→ or ->) ---
    data_flow = shape.get("data_flow", "")
    if isinstance(data_flow, str) and "→" not in data_flow and "->" not in data_flow:
        errors.append("Rule 5: shape.data_flow must contain at least one arrow (→ or ->)")

    # --- Rule 6: Out of scope has a reason for each exclusion ---
    out_of_scope = boundaries.get("out_of_scope", [])
    if isinstance(out_of_scope, list):
        for i, item in enumerate(out_of_scope):
            if isinstance(item, dict):
                reason = item.get("reason")
                if not reason:
                    errors.append(
                        f"Rule 6: boundaries.out_of_scope[{i}] missing 'reason'"
                    )
                elif _is_empty(reason):
                    errors.append(
                        f"Rule 6: boundaries.out_of_scope[{i}] 'reason' is empty or contains placeholder-like content: {reason!r}"
                    )
            elif isinstance(item, str):
                # String entries should be rejected — need structured format
                errors.append(
                    f"Rule 6: boundaries.out_of_scope[{i}] must be an object "
                    f"with 'item' and 'reason', got string"
                )

    # --- Rule 7: Problem describes current pain, not desired future ---
    problem = goal.get("problem", "")
    if isinstance(problem, str):
        future_patterns = re.compile(
            r"^(we\s+(?:want|need|should|would\s+like)|"
            r"it\s+(?:should|would\s+be\s+nice|could))",
            re.IGNORECASE,
        )
        if future_patterns.search(problem.strip()):
            errors.append(
                "Rule 7: goal.problem should describe current pain, "
                "not a desired future state"
            )

    return errors


def validate_files_examined(data: dict) -> list[str]:
    """Validate files_examined entries if present. Returns error strings."""
    errors = []
    fe = data.get("files_examined")
    if fe is None:
        return []  # field is optional
    if not isinstance(fe, list):
        return ["files_examined must be an array"]
    for i, entry in enumerate(fe):
        if not isinstance(entry, dict):
            errors.append(f"files_examined[{i}] must be an object")
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append(f"files_examined[{i}].path must be a non-empty string")
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            errors.append(f"files_examined[{i}].summary must be a non-empty string")
        sha = entry.get("sha")
        if sha is not None:
            if not isinstance(sha, str):
                errors.append(f"files_examined[{i}].sha must be a string or null")
            elif not re.fullmatch(r'[0-9a-fA-F]+', sha):
                errors.append(f"files_examined[{i}].sha must be a hex string, got '{sha}'")
    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate runner spec file")
    parser.add_argument("file", help="Path to spec JSON file")
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

    fe_errors = validate_files_examined(data)
    if fe_errors:
        for err in fe_errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    # Strip top-level metadata fields before validation
    spec_data = {k: v for k, v in data.items() if k not in ("valid", "id", "intake_id", "files_examined")}

    errors = validate(spec_data)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    print("Valid")


if __name__ == "__main__":
    main()
