#!/usr/bin/env python3
"""
Autonomous planner — Stage 3 of the pipeline.

Reads a validated spec JSON, launches a Claude Code subprocess to explore
the codebase and decompose the spec into fine-grained implementation steps,
validates the output, and retries up to 3 times on failure.

Output: pipeline/plans/<uuid>.json

Usage:
    auto_plan.py <path_to_spec.json>
"""
import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
PLANS_DIR = SCRIPT_DIR / "plans"
VALIDATE_SCRIPT = SCRIPT_DIR / "validate_plan.py"

MAX_RETRIES = cfg("plan", "max_retries", 3)

PLAN_SCHEMA = textwrap.dedent("""\
{
  "steps": [
    {
      "step_number": 1,
      "type": "create|modify|delete|test|verify",
      "description": "Human-readable description of what this step does",
      "action": "create|modify|delete|test|verify",
      "files": ["path/to/file.py"],
      "depends_on": [],
      "code_spec": "Detailed pseudocode: what to add/change, function signatures, logic flow. Specific enough that a coding model can translate directly to code without making design decisions."
    }
  ]
}
""")


def build_prompt(spec: dict, previous_errors: list[str] | None = None) -> str:
    """Construct the prompt for the Claude Code subprocess."""
    spec_text = json.dumps(spec, indent=2)

    parts = [
        "You are an autonomous implementation planner. Your task is to decompose "
        "a spec into fine-grained, ordered implementation steps.",
        "",
        "## Spec to decompose",
        "",
        "```json",
        spec_text,
        "```",
        "",
        "## Instructions",
        "",
        "1. Explore the codebase freely — read files, search for patterns, "
        "understand existing architecture and conventions.",
        "2. Decompose the spec into ordered implementation steps. Each step should be "
        "granular enough that a coding model (Sonnet) can implement it without "
        "making design decisions.",
        "3. For each step, write detailed code_spec pseudocode: function signatures, "
        "logic flow, imports, what to add/change. Be specific.",
        "4. For 'modify' and 'delete' actions, the files listed MUST exist in the "
        "codebase. For 'create' actions, the files are new.",
        "5. Always include at least one 'verify' step at the end that describes "
        "how to confirm the implementation works.",
        "6. Ensure every acceptance criterion from the spec is covered by at "
        "least one step's description or code_spec.",
        "7. Set depends_on to reference step_numbers that must complete before "
        "this step can start. No circular dependencies.",
        "",
        "## Output format",
        "",
        "Output ONLY valid JSON matching this schema (no markdown, no explanation):",
        "",
        "```json",
        PLAN_SCHEMA,
        "```",
        "",
        "Valid step types and actions: create, modify, delete, test, verify.",
    ]

    if previous_errors:
        parts.extend([
            "",
            "## Previous attempt failed validation with these errors:",
            "",
        ])
        for err in previous_errors:
            parts.append(f"- {err}")
        parts.append("")
        parts.append("Fix ALL of the above issues in your new output.")

    return "\n".join(parts)


def run_claude(prompt: str) -> tuple[int, str, str]:
    """Run Claude Code subprocess and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["claude", "-p", "-", "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=cfg("plan", "timeout", 300),
    )
    from cost_emit import emit_usage_xml
    emit_usage_xml(result.stdout)
    return result.returncode, result.stdout, result.stderr


def extract_plan(raw_output: str) -> dict:
    """Parse Claude Code output and extract the plan JSON."""
    # Try parsing as Claude JSON envelope
    try:
        envelope = json.loads(raw_output)
        if isinstance(envelope, dict) and "result" in envelope:
            text = envelope["result"]
        elif isinstance(envelope, dict) and "steps" in envelope:
            return envelope
        else:
            text = raw_output
    except json.JSONDecodeError:
        text = raw_output

    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    return json.loads(text)


def validate_plan(plan_path: Path) -> list[str]:
    """Run validate_plan.py and return list of errors (empty = valid)."""
    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(plan_path)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return []
    return [line.strip() for line in result.stderr.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description="Autonomous planner — pipeline stage 3")
    parser.add_argument("spec", help="Path to spec JSON file")
    args = parser.parse_args()

    # Read spec
    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"Spec file not found: {args.spec}", file=sys.stderr)
        sys.exit(1)

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid spec JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not spec.get("valid", False):
        print("Spec is not valid -- cannot plan from invalid spec", file=sys.stderr)
        sys.exit(1)

    spec_id = spec.get("id", spec_path.stem)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    plan_path = PLANS_DIR / f"{spec_id}.json"

    best_plan = None
    best_errors = None
    previous_errors = None

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"Attempt {attempt}/{MAX_RETRIES}...", file=sys.stderr)

        prompt = build_prompt(spec, previous_errors)

        try:
            returncode, stdout, stderr = run_claude(prompt)
        except subprocess.TimeoutExpired:
            print(f"Claude Code error: subprocess timed out (attempt {attempt})", file=sys.stderr)
            previous_errors = ["Claude Code subprocess timed out"]
            continue
        except FileNotFoundError:
            print("Claude Code error: 'claude' command not found", file=sys.stderr)
            sys.exit(1)

        if returncode != 0:
            msg = stderr.strip() or f"exit code {returncode}"
            print(f"Claude Code error: {msg} (attempt {attempt})", file=sys.stderr)
            previous_errors = [f"Claude Code failed: {msg}"]
            continue

        # Parse plan from output
        try:
            plan_data = extract_plan(stdout)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Failed to parse plan JSON (attempt {attempt}): {e}", file=sys.stderr)
            previous_errors = [f"Output was not valid JSON: {e}"]
            continue

        # Assemble full plan with metadata
        plan = {
            "uuid": spec_id,
            "executor_model": cfg("executor", "model", "sonnet"),
            "spec": spec,
            "steps": plan_data.get("steps", []),
        }
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        # Validate
        errors = validate_plan(plan_path)
        if not errors:
            plan["valid"] = True
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            print(str(plan_path))
            sys.exit(0)

        # Track best attempt
        if best_errors is None or len(errors) < len(best_errors):
            best_plan = plan
            best_errors = errors

        previous_errors = errors
        print(f"Validation failed (attempt {attempt}): {len(errors)} error(s)", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)

    # All retries exhausted
    if best_plan:
        best_plan["valid"] = False
        plan_path.write_text(json.dumps(best_plan, indent=2), encoding="utf-8")

    print(f"Failed after {MAX_RETRIES} attempts. Best attempt written to {plan_path}", file=sys.stderr)
    if best_errors:
        for err in best_errors:
            print(f"  {err}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
