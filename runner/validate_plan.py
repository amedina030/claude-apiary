#!/usr/bin/env python3
"""
Validate a plan JSON file for the autonomous runner.

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
VALID_MODELS = {"opus", "sonnet", "haiku"}
REQUIRED_STEP_FIELDS = ["step_number", "type", "description", "action", "files", "depends_on", "code_spec"]

# Words that indicate a test code_spec is prose, not a shell command. The
# executor passes test code_spec directly to subprocess.run(shell=True), so
# 'Run python -m pytest ...' tries to execute literal 'Run' as a binary.
_PROSE_STARTERS = {
    "run", "execute", "use", "call", "then", "now", "this", "make",
    "please", "here", "the", "we", "you",
}

# Banned tokens — substrings that, if found in any step's code_spec or
# description, indicate the planner ignored project conventions documented
# in CLAUDE.md and docs/standards/code-style.md. Hard rule violations only:
# things the codebase forbids outright. Extend this map as new violations
# are caught in runner runs. The reason string is shown to the planner
# on retry so it can correct course.
_BANNED_TOKENS = {
    "pytest": "use unittest (stdlib) — see docs/standards/code-style.md",
    "shell=true": "shell=True is banned — use list-form subprocess args",
    "import requests": "external dependencies are banned — stdlib only",
    "from requests": "external dependencies are banned — stdlib only",
}

# Phrases that, when found in a test-action step's description, indicate the
# planner is using a test step as a gating audit run rather than a pass/fail
# verification. The executor treats every test step as a hard gate (non-zero
# exit aborts the plan), so a "this run is expected to report violations"
# step always aborts. Pattern caught in T5b plan step 3 (#211). The fix is
# either to make the audit step a non-test type, or to make it a test step
# whose code_spec already includes the violation-handling logic.
_TEST_FAILURE_LANGUAGE = (
    "expected to fail",
    "expected to report violations",
    "expected to enumerate violations",
    "expected to error",
    "should fail",
    "this run is expected to",
)


# --- Path allowlist (#212) ---
#
# Plans may legitimately reference files outside the repo working tree —
# specifically persistent state under ~/.claude/projects/<project-key>/, per
# the portability rule that user state lives there. But we still need to
# reject *accidental* absolute paths (T5b had Windows C:\ paths slip through).
#
# Resolution: any absolute path in step.files must, after resolving, fall
# under one of the allowlist roots below. Relative paths are unconditionally
# accepted (they're inherently in-repo when the runner cd's to repo root).

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_project_key() -> str:
    """Read .claude-project-key from repo root, or fall back to a default.

    Mirrors the resolution used by core/utils/project.py but kept local
    so runner stages don't have to import from core (different package).
    """
    key_file = _REPO_ROOT / ".claude-project-key"
    try:
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    except OSError:
        pass
    return "claude-apiary"


def _allowlist_roots() -> list[Path]:
    """Return the list of resolved roots that absolute paths may live under."""
    return [
        _REPO_ROOT.resolve(),
        (Path.home() / ".claude" / "projects" / _read_project_key()).resolve(),
    ]


def _path_under_any(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


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


def _check_test_code_spec_format(steps: list[dict]) -> list[str]:
    """Reject test-action steps whose code_spec is prose, not a shell command.

    The executor's run_test_command does subprocess.run(code_spec, shell=True),
    so the planner MUST emit a single shell command for test steps. Two cheap
    heuristics catch the failure mode that bit T4 step 6 ('Run python -m
    pytest ...' tried to execute literal 'Run' as a Windows binary):

    1. The stripped code_spec must not contain newlines (real commands fit
       on one line; prose almost never does).
    2. The first whitespace-separated token must not be a known prose starter
       like 'Run', 'Execute', 'Use', etc.
    """
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if step.get("action") != "test":
            continue
        code_spec = step.get("code_spec", "")
        if not isinstance(code_spec, str):
            continue
        stripped = code_spec.strip()
        if not stripped:
            # 'empty' is already caught by the required-field check
            continue
        if "\n" in stripped:
            errors.append(
                f"step[{i}] (action='test'): code_spec must be a single shell "
                f"command on one line — no newlines, no prose. The executor "
                f"passes it directly to subprocess.run(shell=True)."
            )
            continue
        first_word = stripped.split(None, 1)[0].rstrip(":,.").lower()
        if first_word in _PROSE_STARTERS:
            errors.append(
                f"step[{i}] (action='test'): code_spec starts with prose word "
                f"'{first_word}' — must begin with the actual shell command "
                f"itself (e.g. 'python -m unittest ...')."
            )
    return errors


def _check_test_failure_language(steps: list[dict]) -> list[str]:
    """Reject test-action steps whose description signals an expected failure.

    The executor treats every test step as a hard pass/fail gate (non-zero
    exit aborts the plan). A test step described as "expected to report
    violations" or "this run is expected to fail" therefore always aborts
    the plan, defeating its purpose. The planner should either drop the
    gating run entirely, mark it as a non-test step type, or wrap it in
    code_spec logic that turns the violations into a real assertion.
    Caught in T5b plan step 3 (#211).
    """
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if step.get("action") != "test":
            continue
        description = step.get("description", "")
        if not isinstance(description, str):
            continue
        lower = description.lower()
        for phrase in _TEST_FAILURE_LANGUAGE:
            if phrase in lower:
                errors.append(
                    f"step[{i}] (action='test'): description signals expected "
                    f"failure ('{phrase}'). Test steps are hard gates — a non-"
                    f"zero exit aborts the plan. Either change the step type "
                    f"away from 'test' or wrap the audit logic in code_spec "
                    f"so it returns 0 on the expected condition."
                )
                break
    return errors


def _check_path_allowlist(steps: list[dict]) -> list[str]:
    """Reject plans whose step.files contain absolute paths outside the
    allowlist roots (#212).

    Catches T5b-style accidents (e.g. raw C:\\Users\\... paths slipping into
    a plan) without blocking the legitimate case of writing persistent
    state under ~/.claude/projects/<project-key>/.

    Relative paths are unconditionally accepted — they're resolved against
    the working tree at execution time, which is always the repo root.
    """
    errors = []
    roots = _allowlist_roots()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        files = step.get("files", [])
        if not isinstance(files, list):
            continue
        for f in files:
            if not isinstance(f, str) or not f:
                continue
            p = Path(f)
            if not p.is_absolute():
                continue
            if not _path_under_any(p, roots):
                errors.append(
                    f"step[{i}]: absolute path '{f}' is outside the allowlist "
                    f"(must be under repo root or ~/.claude/projects/<project-key>/)"
                )
    return errors


def _check_banned_tokens(steps: list[dict]) -> list[str]:
    """Reject plans whose code_spec or description references banned tokens.

    The planner has access to CLAUDE.md and docs/standards/code-style.md and
    is explicitly told the hard rules in the auto_plan prompt. If it still
    proposes pytest, shell=True, or external imports, the validator catches
    it before any code gets written. Errors flow into the existing 3-attempt
    retry loop in auto_plan, so the planner gets a chance to self-correct.
    """
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        haystack = " ".join([
            str(step.get("code_spec", "")),
            str(step.get("description", "")),
        ]).lower()
        for token, reason in _BANNED_TOKENS.items():
            if token in haystack:
                errors.append(
                    f"step[{i}]: banned token '{token}' found in plan — {reason}"
                )
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

        # Optional per-step model field
        if "model" in step:
            model_val = step.get("model")
            if not isinstance(model_val, str):
                errors.append(f"{label}: field 'model' must be a string")
            elif model_val not in VALID_MODELS:
                errors.append(
                    f"{label}: invalid model '{model_val}' "
                    f"(expected: {', '.join(sorted(VALID_MODELS))})"
                )

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

    # Test-action code_spec format check (must be a shell command, not prose)
    errors.extend(_check_test_code_spec_format(steps))

    # Test-action description must not signal expected failure (#211)
    errors.extend(_check_test_failure_language(steps))

    # Banned-token check (project convention violations: pytest, shell=True, etc.)
    errors.extend(_check_banned_tokens(steps))

    # Path allowlist (#212): reject absolute paths outside repo + state dir.
    errors.extend(_check_path_allowlist(steps))

    # Acceptance criteria coverage
    if isinstance(spec, dict):
        errors.extend(_check_criteria_coverage(spec, steps))

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate runner plan file")
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
