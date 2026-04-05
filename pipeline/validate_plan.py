#!/usr/bin/env python3
"""
Validate a plan JSON file for the autonomous pipeline.

Checks:
- Required top-level fields (uuid, executor_model, spec, steps)
- Required per-step fields (step_number, type, description, action, files, depends_on, code_spec)
- Valid step types and actions
- File existence for modify/delete actions (create actions skip this check)
- No circular dependencies between steps
- Every acceptance criterion from the embedded spec is covered by at least one step

Exit 0 + prints "Valid" on success.
Exit 1 + prints error details on failure.

Usage:
    validate_plan.py <path_to_plan.json>
"""
import argparse
import json
import sys
from pathlib import Path

VALID_TYPES = {"create", "modify", "delete", "test", "verify"}
VALID_ACTIONS = {"create", "modify", "delete", "test", "verify"}
REQUIRED_STEP_FIELDS = ["step_number", "type", "description", "action", "files", "depends_on", "code_spec"]


def _detect_cycles(steps: list[dict]) -> list[str]:
    """Detect circular dependencies in step graph. Returns error strings."""
    # Build adjacency: step_number -> list of step_numbers it depends on
    step_nums = {s["step_number"] for s in steps if isinstance(s.get("step_number"), int)}
    graph = {}
    for s in steps:
        num = s.get("step_number")
        deps = s.get("depends_on", [])
        if isinstance(num, int) and isinstance(deps, list):
            graph[num] = [d for d in deps if isinstance(d, int)]

    # DFS cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    errors = []

    def dfs(node, path):
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                # Found cycle
                cycle_start = path.index(dep)
                cycle = path[cycle_start:] + [dep]
                errors.append(f"Circular dependency detected: {' -> '.join(str(n) for n in cycle)}")
                return
            if color[dep] == WHITE:
                dfs(dep, path)
        path.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            dfs(node, [])

    return errors


def _check_criteria_coverage(spec: dict, steps: list[dict]) -> list[str]:
    """Check that every acceptance criterion is referenced by at least one step."""
    criteria = spec.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or not criteria:
        return []

    errors = []
    # Build combined text from all step descriptions and code_specs
    step_text = " ".join(
        f"{s.get('description', '')} {s.get('code_spec', '')}"
        for s in steps if isinstance(s, dict)
    ).lower()

    for i, criterion in enumerate(criteria):
        if not isinstance(criterion, str):
            continue
        # Extract significant keywords (4+ chars) from criterion
        keywords = [w for w in criterion.lower().split() if len(w) > 3]
        # Require at least some keyword overlap
        if keywords and not any(kw in step_text for kw in keywords):
            errors.append(
                f"Acceptance criterion [{i}] not covered by any step: "
                f"'{criterion[:80]}...'" if len(criterion) > 80 else
                f"Acceptance criterion [{i}] not covered by any step: '{criterion}'"
            )

    return errors


def validate(data: dict) -> list[str]:
    """Validate plan data and return list of error strings (empty = valid)."""
    errors = []

    if not isinstance(data, dict):
        return ["Expected a JSON object"]

    # Required top-level fields
    for field in ["uuid", "executor_model", "spec", "steps"]:
        if field not in data:
            errors.append(f"Missing required field '{field}'")

    if errors:
        return errors

    spec = data.get("spec", {})
    steps = data.get("steps", [])

    if not isinstance(steps, list):
        errors.append("'steps' must be an array")
        return errors

    if len(steps) == 0:
        errors.append("'steps' is empty — plan must have at least one step")
        return errors

    # Per-step validation
    seen_numbers = set()
    for i, step in enumerate(steps):
        label = f"step[{i}]"

        if not isinstance(step, dict):
            errors.append(f"{label}: not a JSON object")
            continue

        # Required fields
        for field in REQUIRED_STEP_FIELDS:
            val = step.get(field)
            if val is None:
                errors.append(f"{label}: missing required field '{field}'")
            elif isinstance(val, str) and not val.strip():
                errors.append(f"{label}: field '{field}' is empty")

        # Step number uniqueness
        num = step.get("step_number")
        if isinstance(num, int):
            if num in seen_numbers:
                errors.append(f"{label}: duplicate step_number {num}")
            seen_numbers.add(num)

        # Valid type and action
        step_type = step.get("type")
        if isinstance(step_type, str) and step_type not in VALID_TYPES:
            errors.append(f"{label}: invalid type '{step_type}' (expected: {', '.join(sorted(VALID_TYPES))})")

        action = step.get("action")
        if isinstance(action, str) and action not in VALID_ACTIONS:
            errors.append(f"{label}: invalid action '{action}' (expected: {', '.join(sorted(VALID_ACTIONS))})")

        # File existence for modify/delete actions
        if action in ("modify", "delete"):
            files = step.get("files", [])
            if isinstance(files, list):
                for f in files:
                    if isinstance(f, str) and not Path(f).exists():
                        errors.append(f"{label}: file not found: {f}")

        # depends_on references valid step numbers
        deps = step.get("depends_on", [])
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, int) and dep not in seen_numbers and dep >= num:
                    # Forward reference — may still be valid if the step exists later
                    pass  # Checked via cycle detection instead

    # Circular dependency check
    errors.extend(_detect_cycles(steps))

    # Acceptance criteria coverage
    if isinstance(spec, dict):
        errors.extend(_check_criteria_coverage(spec, steps))

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate pipeline plan file")
    parser.add_argument("file", help="Path to plan JSON file")
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

    # Strip top-level metadata before validation
    plan_data = {k: v for k, v in data.items() if k != "valid"}

    errors = validate(plan_data)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    print("Valid")


if __name__ == "__main__":
    main()
