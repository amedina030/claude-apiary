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
import re
import sys
import textwrap
from pathlib import Path

from .config_loader import get as cfg
from .schema_versions import (
    PLAN_SCHEMA_VERSION,
    SPEC_SCHEMA_VERSION,
    assert_schema_version,
)
from .stage_lib import (
    ClaudeMissingError,
    extract_json,
    retry_until_valid,
    run_validator,
)
from .stage_lib import (
    run_claude as _spawn,
)
from .target_repo import plans_dir

SCRIPT_DIR = Path(__file__).resolve().parent
PLANS_DIR = plans_dir()
REPO_ROOT = SCRIPT_DIR.parent

MAX_RETRIES = cfg("plan", "max_retries", 3)

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _sanitize_prompt_value(value: str, max_length: int = 300) -> str:
    """Strip control characters and truncate to prevent prompt injection from LLM-generated values."""
    sanitized = _CTRL_RE.sub(" ", value).strip()
    return sanitized[:max_length]


def _resolve_banned_tokens_for_prompt(target_repo: str) -> dict:
    """Resolve the banned-tokens map for the prompt's BANNED TOKENS section.

    Delegates to validate_plan._resolve_banned_tokens so the prompt and the
    validator stay in sync — same target, same banned list. Imported lazily
    to avoid a module-level circular import.
    """
    from .validate_plan import _resolve_banned_tokens

    return _resolve_banned_tokens(target_repo)


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
      "code_spec": "Detailed pseudocode: what to add/change, function signatures, logic flow. Specific enough that a coding model can translate directly to code without making design decisions.",
      "post_conditions": [
        {"type": "file_contains", "file": "path/to/file.py", "text": "def new_function"},
        {"type": "file_lacks",    "file": "path/to/file.py", "text": "old_symbol_name"},
        {"type": "file_exists",   "file": "path/to/new_file.py"},
        {"type": "file_absent",   "file": "path/to/deleted_file.py"}
      ]
    }
  ]
}
""")


_APIARY_INSTRUCTION_1 = (
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
    "Do the same for any test action whose code_spec runs a runner test module."
)

_GENERIC_INSTRUCTION_1 = (
    "1. The target repo's coding conventions, test framework, and any hard "
    "rules appear below under '## Target repo conventions' (the target's "
    "CLAUDE.md). Read and follow those rules when deciding on test frameworks, "
    "library choices, naming conventions, module structure, and file I/O "
    "patterns. If conventions specify a banned-token list or forbidden "
    "patterns, plans that violate them will be rejected by the validator."
)


def build_prompt(
    spec: dict,
    previous_errors: list[str] | None = None,
    previous_attempt: dict | None = None,
    *,
    target_repo: str | Path | None = None,
) -> str:
    """Construct the prompt for the Claude Code subprocess.

    ``target_repo`` selects which conventions to embed (phase 4 multi-repo):
      - apiary (or None): keep the historical apiary-specific instruction
        block and the hardcoded banned-tokens list — byte-identical output
        to the pre-phase-4 prompt.
      - non-apiary: swap in a generic instruction pointing at the target's
        CLAUDE.md, inject that file's contents as '## Target repo
        conventions', and render the banned-tokens list resolved from
        runner/config.json per-target overrides (empty section omitted).
    """
    spec_text = json.dumps(spec, indent=2)

    # Resolve target (default = apiary) and whether we're in the apiary case.
    target_path = Path(target_repo) if target_repo else REPO_ROOT
    try:
        is_apiary = target_path.resolve() == REPO_ROOT.resolve()
    except OSError:
        is_apiary = False

    # Read the target's CLAUDE.md for non-apiary runs. The apiary case
    # already references its own CLAUDE.md via the instruction text, so
    # we skip the redundant injection there to preserve byte-identity.
    target_claude_md = None
    if not is_apiary:
        claude_md = target_path / "CLAUDE.md"
        if claude_md.exists() and claude_md.is_file():
            try:
                target_claude_md = claude_md.read_text(encoding="utf-8")
            except OSError:
                target_claude_md = None

    # Resolve banned tokens for this target via the same config path
    # validate_plan uses, so the instruction text and the validator stay
    # in sync. Empty dict -> omit the BANNED TOKENS section entirely.
    banned_tokens = _resolve_banned_tokens_for_prompt(str(target_path))

    # Pre-compute files_examined so instruction 2 can reference pre-read files (ATK-002)
    files_examined_raw = spec.get("files_examined") or []
    seen_paths: set[str] = set()
    unique_entries: list[dict] = []
    for _entry in files_examined_raw:
        _p = _entry.get("path", "")
        if _p and _p not in seen_paths:
            seen_paths.add(_p)
            unique_entries.append(_entry)

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
        _APIARY_INSTRUCTION_1 if is_apiary else _GENERIC_INSTRUCTION_1,
        (
            "2. The files listed under 'Files already examined' below were read "
            "during spec-writing and do NOT count against the search budget — skip "
            "re-reading them unless you need to verify behavior that may have changed. "
            "For any other files the spec requires, use Grep/Glob sparingly — at most "
            "3 search queries total, and prefer narrow glob patterns over broad content "
            "searches. Do not do exploratory reading of unrelated parts of the codebase."
            if unique_entries
            else "2. Read only the specific files mentioned in the spec (e.g. "
            "files_to_modify, related_files, or files referenced in acceptance "
            "criteria). If you must locate something the spec does not name, use "
            "Grep/Glob sparingly — at most 3 search queries total, and prefer "
            "narrow glob patterns over broad content searches. Do not do "
            "exploratory reading of unrelated parts of the codebase: every extra "
            "file you open is charged against this stage's token budget."
        ),
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
        "5a. SYMBOL REMOVAL RULE: When a step removes, renames, or deletes a "
        "function, class, constant, or import from a file, you MUST grep the "
        "repo for ALL references to that symbol (imports AND usage sites) and "
        "include update steps for EVERY file that references it. Do not rely "
        "on the spec's file list — it may be incomplete. Use "
        "'Grep(pattern=\"SYMBOL_NAME\")' to find all references. Missing even "
        "one file will cause NameError/ImportError in the test suite. The "
        "validator will reject plans that remove a symbol without covering "
        "all files that reference it.",
        "5b. DOC GATE: the target repo may map code files to architecture "
        "docs (docs/change_map.json -- read it if it exists; reading it does "
        "not count against the search budget). The executor commits ONE "
        "STEP AT A TIME and the repo's commit hook checks EVERY commit, so "
        "each create/modify/delete step whose files touch mapped code MUST "
        "itself satisfy the gate: either (a) include one of the mapped doc "
        "files in that SAME step's files list, updating it in the same "
        "commit and bumping its `last_verified:` frontmatter date to today, "
        'or (b) set "docs_unchanged": true on that step -- only after '
        "confirming the doc's claims are genuinely unaffected. A doc "
        "updated in a different step does NOT unblock the commit. The "
        "validator rejects per-step violations, and the commit hook would "
        "block the executor anyway.",
        "6. Always include at least one 'verify' step at the end that describes "
        "how to confirm the implementation works.",
        "7. Ensure every acceptance criterion from the spec is covered by at "
        "least one step's description or code_spec.",
        "8. Set depends_on to reference step_numbers that must complete before "
        "this step can start. No circular dependencies. IMPORTANT: if two or "
        "more steps target the same file, they MUST be linked by a depends_on "
        "chain (the later step must list the earlier step's step_number in its "
        "depends_on). Without this, the executor may run them in either order "
        "and one step's edits will clobber the other's. The validator rejects "
        "plans with unlinked file overlaps.",
        "9. For every create/modify/delete step, include post_conditions "
        "describing the observable end-state of the step — the executor "
        "verifies these against the filesystem AFTER the step's subprocess "
        "returns, regardless of which step's subprocess actually made the "
        "change. This lets a naturally-coupled pair of steps (e.g. 'add "
        "function' + 'wire function into caller') both succeed even when "
        "the first step's subprocess does both edits: step 2's "
        "post_conditions are already true, so the runner accepts it as a "
        "no-op success instead of aborting with 'no changes'. Supported "
        "condition types: "
        "'file_contains' (file must contain text — requires 'file' + 'text'), "
        "'file_lacks' (file must NOT contain text — for symbol removal), "
        "'file_exists' (file must be present — requires 'file'), "
        "'file_absent' (file must be gone — for delete actions). "
        "Pick anchor texts that are specific enough to avoid false positives "
        "(e.g. the full 'def symbol_name(' rather than just 'symbol_name'). "
        "Post_conditions are optional for test/verify steps.",
        "",
    ]

    if is_apiary:
        # Byte-identical apiary-case banned-token block (phase 4: this MUST
        # match the pre-refactor prompt for the apiary-self acceptance test).
        parts.extend(
            [
                "## BANNED TOKENS — the validator auto-rejects plans containing these:",
                "",
                "- 'pytest' → use 'python -m unittest <module>' instead",
                "- 'shell=true' → use list-form subprocess args instead",
                "- 'import requests' / 'from requests' → stdlib only (use urllib)",
                "",
                "These apply to ALL fields: description, code_spec, and file names.",
                "Even one occurrence anywhere in the plan causes automatic rejection.",
                "",
            ]
        )
    elif banned_tokens:
        # Non-apiary with a configured override: render the configured list.
        parts.append("## BANNED TOKENS — the validator auto-rejects plans containing these:")
        parts.append("")
        for token, reason in banned_tokens.items():
            parts.append(f"- '{token}' → {reason}")
        parts.extend(
            [
                "",
                "These apply to ALL fields: description, code_spec, and file names.",
                "Even one occurrence anywhere in the plan causes automatic rejection.",
                "",
            ]
        )
    # Non-apiary with no configured override: no banned-tokens section at all.

    parts.extend(
        [
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
    )

    # Inject the target repo's CLAUDE.md for non-apiary runs so the planner
    # can consult the actual target conventions without a separate file read.
    if target_claude_md is not None:
        parts.extend(
            [
                "",
                "## Target repo conventions",
                "",
                "The following is the target repository's CLAUDE.md. Treat these "
                "as the hard rules for this plan (test framework, imports, style, "
                "forbidden patterns). If it conflicts with generic advice above, "
                "the target's CLAUDE.md wins.",
                "",
                target_claude_md.rstrip(),
                "",
            ]
        )

    # Inject file-trust context if spec has files_examined
    if unique_entries:
        parts.extend(
            [
                "",
                "## Files already examined by the refiner",
                "",
                "The following files were read during the spec-writing stage. "
                "Treat them as already-read context — do NOT re-read these files "
                "unless you need to verify behavior that may have changed.",
                "",
            ]
        )
        for entry in unique_entries:
            # Sanitize LLM-generated values to prevent prompt injection (ATK-001)
            path = _sanitize_prompt_value(entry.get("path", ""))
            summary = _sanitize_prompt_value(entry.get("summary", ""))
            sha = entry.get("sha")
            sha_note = f" (sha: {sha})" if sha else " (sha: unknown — content may have changed)"
            parts.append(f"- `{path}`{sha_note}: {summary}")
        parts.append("")

    if previous_attempt and previous_errors:
        # Retry 2026-08-31: attempt 3 fixed the one named error but
        # regenerated the plan from scratch and dropped attempt 2's
        # change-map attestations. Show the model its own previous plan and
        # demand a minimal edit so already-passing fixes survive the retry.
        parts.extend(
            [
                "",
                "## Your previous plan attempt",
                "",
                "```json",
                json.dumps({"steps": previous_attempt.get("steps", [])}, indent=2),
                "```",
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
        if previous_attempt:
            parts.append(
                "Start from your previous plan above and apply the SMALLEST "
                "edit that fixes ALL of the listed errors. Keep every other "
                "step, field, depends_on link, and docs_unchanged "
                "attestation exactly as it was -- regenerating from scratch "
                "loses fixes that already passed validation."
            )
        else:
            parts.append("Fix ALL of the above issues in your new output.")

    return "\n".join(parts)


def run_claude(prompt: str) -> tuple[int, str, str]:
    """Run Claude Code subprocess and return (returncode, stdout, stderr)."""
    return _spawn(prompt, timeout=cfg("plan", "timeout", 900), model=cfg("plan", "model", "opus"))


def extract_plan(raw_output: str) -> dict:
    """Parse Claude Code output and extract the plan JSON.

    Thin alias over the one shared salvager (``stage_lib.extract_json``), which
    handles every failure mode LLM-generated JSON has produced here: the Claude
    envelope, prose before/after the block, markdown fences (possibly nested
    inside a ``code_spec`` string), and unescaped newlines/tabs inside string
    values. An object carrying ``steps`` wins over any other object found.
    """
    return extract_json(raw_output, require_keys=("steps",), allow_list=False)


_MERGEABLE_ACTIONS = {"create", "modify"}


def _merge_subsumed_steps(steps: list[dict]) -> list[dict]:
    """Collapse consecutive steps where step N+1 is subsumed by step N.

    Rule: merge step B into step A when
      - both A.action and B.action are in {create, modify},
      - B.depends_on == [A.step_number] (B depends solely on A),
      - set(B.files) ⊆ set(A.files) (B touches no file A doesn't already own).

    When merged, B's description is appended to A's description, B's code_spec
    is appended to A's code_spec under an "Additionally:" header, B's
    post_conditions are unioned onto A's, and every other step referencing B
    in its depends_on is rewritten to reference A. Steps are then renumbered
    contiguously from 1.

    The pass runs iteratively until no more merges are possible, so a chain
    A -> B -> C all targeting the same file collapses to a single step.

    Motivation: the planner often emits naturally-atomic pairs ("add
    function" + "wire it into caller in same file") as two separate steps,
    which doubles executor subprocess count and token spend. See
    T-2026-127.
    """
    if not isinstance(steps, list):
        return steps

    def _files_set(step: dict) -> set:
        files = step.get("files", [])
        if not isinstance(files, list):
            return set()
        return {f.replace("\\", "/").strip() for f in files if isinstance(f, str) and f}

    def _one_pass(current: list[dict]) -> tuple[list[dict], bool]:
        for i in range(len(current) - 1):
            a = current[i]
            b = current[i + 1]
            if not (isinstance(a, dict) and isinstance(b, dict)):
                continue
            if a.get("action") not in _MERGEABLE_ACTIONS:
                continue
            if b.get("action") not in _MERGEABLE_ACTIONS:
                continue
            a_num = a.get("step_number")
            if not isinstance(a_num, int):
                continue
            if b.get("depends_on") != [a_num]:
                continue
            if not _files_set(b).issubset(_files_set(a)):
                continue

            b_num = b.get("step_number")
            merged = dict(a)
            merged["description"] = (
                f"{a.get('description', '')} + {b.get('description', '')}"
            ).strip(" +")
            a_spec = a.get("code_spec", "") or ""
            b_spec = b.get("code_spec", "") or ""
            merged["code_spec"] = (
                f"{a_spec}\n\nAdditionally:\n{b_spec}" if a_spec and b_spec else (a_spec or b_spec)
            )
            a_pc = a.get("post_conditions") or []
            b_pc = b.get("post_conditions") or []
            seen = []
            for pc in list(a_pc) + list(b_pc):
                if pc not in seen:
                    seen.append(pc)
            if seen:
                merged["post_conditions"] = seen

            new_steps = current[:i] + [merged] + current[i + 2 :]
            for s in new_steps:
                if not isinstance(s, dict):
                    continue
                deps = s.get("depends_on")
                if isinstance(deps, list):
                    s["depends_on"] = [a_num if d == b_num else d for d in deps]
            return new_steps, True
        return current, False

    current = list(steps)
    changed = True
    while changed:
        current, changed = _one_pass(current)

    # Renumber contiguously from 1, preserving order, and remap depends_on.
    renumber = {}
    for new_idx, s in enumerate(current, start=1):
        if isinstance(s, dict) and isinstance(s.get("step_number"), int):
            renumber[s["step_number"]] = new_idx
    for s in current:
        if not isinstance(s, dict):
            continue
        old = s.get("step_number")
        if isinstance(old, int) and old in renumber:
            s["step_number"] = renumber[old]
        deps = s.get("depends_on")
        if isinstance(deps, list):
            s["depends_on"] = [renumber.get(d, d) for d in deps]
    return current


def validate_plan(plan_path: Path) -> list[str]:
    """Run validate_plan.py and return list of errors (empty = valid)."""
    return run_validator("runner.validate_plan", plan_path, cwd=REPO_ROOT)


def _read_spec(path: Path) -> dict:
    """Load the spec artifact and enforce the stage's preconditions."""
    if not path.exists():
        print(f"Spec file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid spec JSON: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        assert_schema_version(spec, "spec", SPEC_SCHEMA_VERSION)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not spec.get("valid", False):
        print("Spec is not valid -- cannot plan from invalid spec", file=sys.stderr)
        sys.exit(1)
    return spec


def _assemble_plan(plan_data: dict, spec: dict, spec_id: str) -> dict:
    """Build the plan artifact around the model's raw step list."""
    # Collapse naturally-atomic consecutive steps (T-2026-127) before
    # validation so the file-overlap check sees the merged shape.
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "uuid": spec_id,
        "executor_model": cfg("executor", "model", "sonnet"),
        "spec": spec,
        "steps": _merge_subsumed_steps(plan_data.get("steps", [])),
    }
    # Phase 4: propagate target_repo so validate_plan can resolve the
    # correct banned-tokens list and repo root for this target.
    spec_target = spec.get("target_repo")
    if isinstance(spec_target, str) and spec_target.strip():
        plan["target_repo"] = spec_target.strip()
    return plan


def _write(path: Path, artifact: dict) -> None:
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Autonomous planner — runner stage 3")
    parser.add_argument("spec", help="Path to spec JSON file")
    args = parser.parse_args()

    spec_path = Path(args.spec)
    spec = _read_spec(spec_path)

    spec_id = spec.get("id", spec_path.stem)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    plan_path = PLANS_DIR / f"{spec_id}.json"

    # Phase 4: pick the target repo for this run so build_prompt can inject
    # the right CLAUDE.md and banned-tokens section. Spec's target_repo
    # field is propagated from the intake by auto_refine; None = apiary.
    spec_target_repo = spec.get("target_repo")

    def _report(message: str) -> None:
        print(message, file=sys.stderr)

    try:
        ok, best_plan, best_errors = retry_until_valid(
            build_prompt=lambda errors, prev: build_prompt(
                spec, errors, previous_attempt=prev, target_repo=spec_target_repo
            ),
            call_model=run_claude,
            parse=extract_plan,
            assemble=lambda data: _assemble_plan(data, spec, spec_id),
            persist=lambda plan: _write(plan_path, plan),
            validate=lambda: validate_plan(plan_path),
            max_attempts=MAX_RETRIES,
            report=_report,
        )
    except ClaudeMissingError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if ok:
        best_plan["valid"] = True
        _write(plan_path, best_plan)
        print(str(plan_path))
        sys.exit(0)

    # All retries exhausted
    if best_plan:
        best_plan["valid"] = False
        _write(plan_path, best_plan)

    print(
        f"Failed after {MAX_RETRIES} attempts. Best attempt written to {plan_path}", file=sys.stderr
    )
    for err in best_errors:
        print(f"  {err}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
