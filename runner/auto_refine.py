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
import sys
import textwrap
from pathlib import Path

from .config_loader import get as cfg
from .schema_versions import SPEC_SCHEMA_VERSION
from .stage_lib import (
    ClaudeMissingError,
    extract_json,
    retry_until_valid,
    run_validator,
)
from .stage_lib import (
    run_claude as _spawn,
)
from .target_repo import specs_dir

SCRIPT_DIR = Path(__file__).resolve().parent
SPECS_DIR = specs_dir()
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

    parts.extend(
        [
            "",
            "## Instructions",
            "",
        ]
    )
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
    parts.extend(
        [
            "2. Based on your exploration and the task description, produce a spec "
            "that covers all aspects needed for implementation.",
            "3. As you explore the codebase, keep track of every file you read. "
            "For each file, record an entry in the files_examined array with: "
            "'path' (relative to repo root), 'sha' (the git SHA of the file if "
            "available, or null), and 'summary' (a one-line description of what "
            "you learned from this file that is relevant to the spec). Include "
            "ALL files you read during exploration, not just the ones directly "
            "mentioned in the spec. This field is optional but strongly "
            "encouraged — it helps downstream stages avoid redundant file reads.",
            "4. Output ONLY valid JSON matching this schema (no markdown, no explanation):",
            "",
            "```json",
            SPEC_SCHEMA,
            "```",
            "",
            VALIDATION_RULES,
        ]
    )

    if previous_errors:
        parts.extend(
            [
                "",
                "## Previous attempt failed validation with these errors:",
                "",
            ]
        )
        for err in previous_errors:
            parts.append(f"- {err}")
        parts.append("")
        parts.append("Fix ALL of the above issues in your new output.")

    return "\n".join(parts)


def run_claude(prompt: str) -> tuple[int, str, str]:
    """Run Claude Code subprocess and return (returncode, stdout, stderr)."""
    return _spawn(
        prompt, timeout=cfg("refine", "timeout", 900), model=cfg("refine", "model", "opus")
    )


def extract_spec(raw_output: str) -> dict:
    """Parse Claude Code output and extract the spec JSON.

    Thin alias over the one shared salvager (``stage_lib.extract_json``): the
    envelope, markdown fences, prose around the JSON and unescaped newlines
    inside string values are all handled there, in one place, for every stage.
    """
    return extract_json(
        raw_output,
        require_keys=("goal", "shape", "behavior"),
        allow_list=False,
    )


def validate_spec(spec_path: Path) -> list[str]:
    """Run validate_spec.py and return list of errors (empty = valid)."""
    return run_validator("runner.validate_spec", spec_path, cwd=REPO_ROOT)


def _read_intake(path: Path) -> dict:
    """Load the intake artifact or exit with a message."""
    if not path.exists():
        print(f"Intake file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid intake JSON: {e}", file=sys.stderr)
        sys.exit(1)


def _assemble_spec(spec: dict, intake: dict, intake_id: str) -> dict:
    """Stamp the metadata the downstream stages rely on onto a parsed spec."""
    spec["schema_version"] = SPEC_SCHEMA_VERSION
    spec["id"] = intake_id
    spec["intake_id"] = intake_id
    # Phase 4: propagate target_repo intake field onto the spec so
    # downstream stages (auto_plan, validate_plan) can use it without
    # having to walk back to the intake file themselves.
    intake_target = intake.get("target_repo")
    if isinstance(intake_target, str) and intake_target.strip():
        spec["target_repo"] = intake_target.strip()
    return spec


def _write(path: Path, artifact: dict) -> None:
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Autonomous refiner — runner stage 2")
    parser.add_argument("intake", help="Path to intake JSON file")
    args = parser.parse_args()

    intake_path = Path(args.intake)
    intake = _read_intake(intake_path)

    intake_id = intake.get("id", intake_path.stem)
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = SPECS_DIR / f"{intake_id}.json"

    def _report(message: str) -> None:
        print(message, file=sys.stderr)

    try:
        ok, best_spec, best_errors = retry_until_valid(
            # _prev (the prior spec artifact) is accepted but unused: refine
            # keeps error-only feedback until it shows the same regression.
            build_prompt=lambda errors, _prev: build_prompt(intake, errors),
            call_model=run_claude,
            parse=extract_spec,
            assemble=lambda spec: _assemble_spec(spec, intake, intake_id),
            persist=lambda spec: _write(spec_path, spec),
            validate=lambda: validate_spec(spec_path),
            max_attempts=MAX_RETRIES,
            report=_report,
        )
    except ClaudeMissingError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if ok:
        best_spec["valid"] = True
        _write(spec_path, best_spec)
        print(str(spec_path))
        sys.exit(0)

    # All retries exhausted — write best attempt with valid: false
    if best_spec:
        best_spec["valid"] = False
        _write(spec_path, best_spec)

    print(
        f"Failed after {MAX_RETRIES} attempts. Best attempt written to {spec_path}", file=sys.stderr
    )
    for err in best_errors:
        print(f"  {err}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
