#!/usr/bin/env python3
"""
Autonomous planner — Stage 3 of the runner.

Reads a validated spec JSON, launches a Claude Code subprocess to explore
the codebase and decompose the spec into fine-grained implementation steps,
validates the output, and retries up to 3 times on failure.

Output: runner/plans/<uuid>.json

Usage:
    auto_plan.py <path_to_spec.json>
"""
import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from .config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
PLANS_DIR = SCRIPT_DIR / "plans"
REPO_ROOT = SCRIPT_DIR.parent

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
        "1. ALWAYS read CLAUDE.md (project root) and docs/standards/code-style.md "
        "BEFORE deciding on test frameworks, library choices, naming conventions, "
        "module structure, or file I/O patterns. These two files document hard "
        "rules in this codebase: stdlib only (no external dependencies), "
        "use unittest (no pytest), explicit encoding='utf-8' on file I/O, "
        "pathlib.Path end-to-end (no string concatenation), no shell=True, no "
        "absolute paths. Reading these two files does NOT count against the "
        "search budget below. A plan that proposes pytest, requests, or any "
        "external dependency will be rejected by the validator. "
        "RUNNER-PACKAGE IMPORT CONVENTION: when generating test files under "
        "runner/ (e.g. runner/test_<module>.py) that will be run as "
        "'python -m unittest runner.test_<module>', do NOT use bare imports "
        "of sibling runner modules — `import detached_lib` will fail with "
        "ModuleNotFoundError because runner/ is a package with relative imports. "
        "Use `from runner import detached_lib` or `from runner.detached_lib import ...` "
        "for test imports. Entry points are `python -m runner.X`, not `python runner/X.py`. "
        "Do the same for any test action whose code_spec runs a runner test module.",
        "2. Read only the specific files mentioned in the spec (e.g. "
        "files_to_modify, related_files, or files referenced in acceptance "
        "criteria). If you must locate something the spec does not name, use "
        "Grep/Glob sparingly — at most 3 search queries total, and prefer "
        "narrow glob patterns over broad content searches. Do not do "
        "exploratory reading of unrelated parts of the codebase: every extra "
        "file you open is charged against this stage's token budget.",
        "3. Decompose the spec into ordered implementation steps. Each step should be "
        "granular enough that a coding model (Sonnet) can implement it without "
        "making design decisions.",
        "4. For each step, write a code_spec whose format depends on the action:",
        "   - action='create'/'modify'/'delete': freeform pseudocode — function "
        "signatures, logic flow, imports, what to add/change. Be specific.",
        "   - action='test': a SINGLE shell command on one line, with NO "
        "surrounding prose. Example: 'python -m unittest runner.test_foo'. "
        "Do NOT write 'Run the tests with python -m unittest...' or any other "
        "prose. The executor passes code_spec directly to "
        "subprocess.run(shell=True), so any prose becomes a shell command and "
        "fails (e.g. 'Run' is tried as a Windows binary).",
        "   - action='verify': the verification check description (what to "
        "confirm and how). The executor passes it to a Claude verify call.",
        "5. For 'modify' and 'delete' actions, the files listed MUST exist in the "
        "codebase. For 'create' actions, the files are new.",
        "6. Always include at least one 'verify' step at the end that describes "
        "how to confirm the implementation works.",
        "7. Ensure every acceptance criterion from the spec is covered by at "
        "least one step's description or code_spec.",
        "8. Set depends_on to reference step_numbers that must complete before "
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
    from .claude_subprocess import run_claude as _spawn
    return _spawn(prompt, timeout=cfg("plan", "timeout", 300))


def _sanitize_json_newlines(text: str) -> str:
    """Escape literal newlines inside JSON string values.

    LLMs often produce JSON with unescaped newlines in string fields
    (especially multi-line code_spec values). This walks the text
    character-by-character, tracking whether we're inside a JSON string,
    and replaces literal newlines inside strings with \\n.
    """
    result = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and in_string:
            # Escaped character — pass through both chars
            result.append(ch)
            if i + 1 < len(text):
                i += 1
                result.append(text[i])
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
        if ch == '\n' and in_string:
            result.append('\\n')
        elif ch == '\r' and in_string:
            pass  # drop \r, the \n that follows will be escaped
        elif ch == '\t' and in_string:
            result.append('\\t')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def extract_plan(raw_output: str) -> dict:
    """Parse Claude Code output and extract the plan JSON.

    Handles multiple failure modes from LLM-generated JSON:
    - Response wrapped in Claude JSON envelope (--output-format json)
    - Prose before/after JSON block
    - Markdown code fences (possibly nested inside code_spec strings)
    - Unescaped newlines/tabs inside JSON string values
    """
    # Step 1: unwrap Claude JSON envelope
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

    # Step 2: try raw_decode on sanitized text (skips fence issues entirely)
    # This is the most reliable path: sanitize newlines, then find the first
    # valid JSON object by scanning for '{'. Works even with prose and nested
    # fences because raw_decode uses the JSON parser's own bracket matching.
    decoder = json.JSONDecoder()
    for candidate in [_sanitize_json_newlines(text), text]:
        for i, ch in enumerate(candidate):
            if ch == '{':
                try:
                    obj, _ = decoder.raw_decode(candidate, i)
                    if isinstance(obj, dict) and "steps" in obj:
                        return obj
                except json.JSONDecodeError:
                    continue

    # Step 3: if raw_decode didn't find a {"steps":...}, try any JSON object
    for candidate in [_sanitize_json_newlines(text), text]:
        for i, ch in enumerate(candidate):
            if ch == '{':
                try:
                    obj, _ = decoder.raw_decode(candidate, i)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue

    raise json.JSONDecodeError("No valid JSON found in output", text, 0)


def validate_plan(plan_path: Path) -> list[str]:
    """Run validate_plan.py and return list of errors (empty = valid)."""
    result = subprocess.run(
        [sys.executable, "-m", "runner.validate_plan", str(plan_path)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if result.returncode == 0:
        return []
    return [line.strip() for line in result.stderr.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description="Autonomous planner — runner stage 3")
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
