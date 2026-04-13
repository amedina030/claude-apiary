#!/usr/bin/env python3
"""
Autonomous refiner — Stage 2 of the runner.

Reads a validated intake JSON, launches a Claude Code subprocess to explore
the codebase and produce a structured spec, validates the output against
the 8 handoff rules, and retries up to 3 times on failure.

Output: runner/specs/<uuid>.json

Usage:
    auto_refine.py <path_to_intake.json>
"""
import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from .config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
SPECS_DIR = SCRIPT_DIR / "specs"
REPO_ROOT = SCRIPT_DIR.parent

MAX_RETRIES = cfg("refine", "max_retries", 3)

SPEC_SCHEMA = textwrap.dedent("""\
{
  "goal": {
    "problem": "What is broken or painful today (1 sentence)",
    "solution": "What this change does about it (1 sentence)",
    "value": "Who benefits and how (1 sentence)"
  },
  "shape": {
    "components": [{"name": "component name", "description": "what it does and why"}],
    "integration_point": "Where this plugs into the existing system",
    "pattern": "What existing pattern it follows",
    "data_flow": "Input source → processing steps → output destination",
    "dependencies": "What this requires to exist/work"
  },
  "behavior": {
    "input": "What the feature receives (type, format, source)",
    "processing": "Ordered steps from input to output",
    "output": "What the feature produces (type, format, destination)",
    "error_cases": [{"trigger": "what goes wrong", "behavior": "expected response"}],
    "edge_cases": [{"condition": "unusual situation", "behavior": "expected response"}]
  },
  "boundaries": {
    "in_scope": ["what is included"],
    "out_of_scope": [{"item": "what is excluded", "reason": "why"}],
    "must_not_break": ["invariants this change must preserve"]
  },
  "acceptance_criteria": [
    "Given [precondition], when [action], then [observable result]"
  ],
  "files_examined": [
    {"path": "relative/path/to/file.py", "sha": "hex-sha-or-null", "summary": "One-line description of what was learned from this file"}
  ]
}
""")

VALIDATION_RULES = textwrap.dedent("""\
Before producing the spec, verify all 8 rules:
1. Every acceptance criterion references a specific input and observable output — no "works correctly" or "handles gracefully"
2. Every error case in behavior has a corresponding acceptance criterion
3. Every edge case in behavior has a corresponding acceptance criterion
4. shape.components lists at least one component with a description
5. shape.data_flow contains at least one arrow (→)
6. boundaries.out_of_scope has a reason for each exclusion
7. goal.problem describes a current pain, not a desired future state
8. No field is left empty or filled with a placeholder
""")


def build_prompt(intake: dict, previous_errors: list[str] | None = None) -> str:
    """Construct the prompt for the Claude Code subprocess."""
    parts = [
        "You are an autonomous spec writer. Your task is to explore this codebase "
        "and produce a detailed, structured spec JSON for the following task.",
        "",
        "## Task from intake",
        f"**Title:** {intake.get('title', '')}",
        f"**Problem:** {intake.get('problem', '')}",
        f"**Description:** {intake.get('description', '')}",
        f"**Scope:** {intake.get('scope', '')}",
    ]

    context = intake.get("context", "")
    if context:
        parts.append(f"**Additional context:** {context}")

    explore_hints = intake.get("explore_hints") or []

    parts.extend([
        "",
        "## Instructions",
        "",
    ])
    if explore_hints:
        parts.append(
            "1. Start by reading these files (they are the most relevant "
            "starting points; branch out from there as needed):"
        )
        for h in explore_hints:
            parts.append(f"   - {h}")
        parts.append(
            "   Then explore further as the task requires — read related files, "
            "search for patterns, understand surrounding architecture."
        )
    else:
        parts.append(
            "1. Explore the codebase freely — read files, search for patterns, "
            "understand the existing architecture."
        )
    parts.extend([
        "2. Based on your exploration and the task description, produce a spec "
        "that covers all aspects needed for implementation.",
        "3. Output ONLY valid JSON matching this schema (no markdown, no explanation):",
        "",
        "```json",
        SPEC_SCHEMA,
        "```",
        "",
        VALIDATION_RULES,
    ])

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
    from .claude_subprocess import run_claude as _spawn
    return _spawn(prompt, timeout=cfg("refine", "timeout", 300), model=cfg("refine", "model", None))


def extract_spec(raw_output: str) -> dict:
    """Parse Claude Code output and extract the spec JSON.

    The --output-format json wraps the response in a JSON envelope with a
    "result" field containing the text. The text itself should be the spec JSON,
    but may be wrapped in markdown code fences.
    """
    # First try: parse the raw output as the Claude JSON envelope
    try:
        envelope = json.loads(raw_output)
        # If it's the envelope format, extract the result text
        if isinstance(envelope, dict) and "result" in envelope:
            text = envelope["result"]
        elif isinstance(envelope, dict) and all(
            k in envelope for k in ("goal", "shape", "behavior")
        ):
            # Already the spec itself
            return envelope
        else:
            text = raw_output
    except json.JSONDecodeError:
        text = raw_output

    # Extract JSON from markdown code fences (may appear after prose)
    import re
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Sanitize unescaped newlines in strings and retry
    from .auto_plan import _sanitize_json_newlines
    sanitized = _sanitize_json_newlines(text)
    try:
        return json.loads(sanitized)
    except json.JSONDecodeError:
        pass

    # Fall back to raw_decode: scan for first valid JSON object
    decoder = json.JSONDecoder()
    for candidate in [sanitized, text]:
        for i, ch in enumerate(candidate):
            if ch == '{':
                try:
                    obj, _ = decoder.raw_decode(candidate, i)
                    return obj
                except json.JSONDecodeError:
                    continue

    raise json.JSONDecodeError("No valid JSON found in output", text, 0)


def validate_spec(spec_path: Path) -> list[str]:
    """Run validate_spec.py and return list of errors (empty = valid)."""
    result = subprocess.run(
        [sys.executable, "-m", "runner.validate_spec", str(spec_path)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if result.returncode == 0:
        return []
    return [line.strip() for line in result.stderr.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description="Autonomous refiner — runner stage 2")
    parser.add_argument("intake", help="Path to intake JSON file")
    args = parser.parse_args()

    # Read intake
    intake_path = Path(args.intake)
    if not intake_path.exists():
        print(f"Intake file not found: {args.intake}", file=sys.stderr)
        sys.exit(1)

    try:
        intake = json.loads(intake_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid intake JSON: {e}", file=sys.stderr)
        sys.exit(1)

    intake_id = intake.get("id", intake_path.stem)
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = SPECS_DIR / f"{intake_id}.json"

    best_spec = None
    best_errors = None
    previous_errors = None

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"Attempt {attempt}/{MAX_RETRIES}...", file=sys.stderr)

        prompt = build_prompt(intake, previous_errors)

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

        # Parse spec from output
        try:
            spec = extract_spec(stdout)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Failed to parse spec JSON (attempt {attempt}): {e}", file=sys.stderr)
            previous_errors = [f"Output was not valid JSON: {e}"]
            continue

        # Write spec for validation
        spec["id"] = intake_id
        spec["intake_id"] = intake_id
        spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

        # Validate
        errors = validate_spec(spec_path)
        if not errors:
            spec["valid"] = True
            spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
            print(str(spec_path))
            sys.exit(0)

        # Track best attempt (fewest errors)
        if best_errors is None or len(errors) < len(best_errors):
            best_spec = spec
            best_errors = errors

        previous_errors = errors
        print(f"Validation failed (attempt {attempt}): {len(errors)} error(s)", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)

    # All retries exhausted — write best attempt with valid: false
    if best_spec:
        best_spec["valid"] = False
        spec_path.write_text(json.dumps(best_spec, indent=2), encoding="utf-8")

    print(f"Failed after {MAX_RETRIES} attempts. Best attempt written to {spec_path}", file=sys.stderr)
    if best_errors:
        for err in best_errors:
            print(f"  {err}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
