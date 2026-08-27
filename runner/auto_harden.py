#!/usr/bin/env python3
"""
Autonomous hardener — Stage 5 of the runner.

Wraps the existing harden/ infrastructure to run attack-defend rounds against
the executor's changed files on the runner branch.

Produces a verdict:
  - all_resolved: safe to auto-merge
  - has_unresolved: needs human review

Output: runner/hardens/<uuid>.json

Usage:
    auto_harden.py <path_to_execution_log.json>
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .config_loader import get as cfg

from .target_repo import hardens_dir, plans_dir

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
HARDENS_DIR = hardens_dir()
HARDEN_DIR = REPO_DIR / "harden"

VALIDATE_FINDINGS = HARDEN_DIR / "validate_findings.py"
VALIDATE_RESPONSE = HARDEN_DIR / "validate_response.py"
ASSIGN_IDS = HARDEN_DIR / "assign_ids.py"

MAX_ROUNDS = cfg("harden", "max_rounds", 3)


# -- Git helpers (#253: shared via runner/git_lib.py) --

from .git_lib import branch_exists, checkout, current_branch as get_current_branch, git
from .schema_versions import (
    EXECUTION_SCHEMA_VERSION,
    HARDEN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    assert_schema_version,
)
from .stage_lib import (
    extract_json_str as extract_json_from_text,
    extract_text,
    iter_unique,
    run_claude as _spawn,
)


def untracked_files() -> set[str]:
    """Untracked, non-ignored files in the checkout (``git ls-files -o``)."""
    result = git("ls-files", "--others", "--exclude-standard", "-z")
    if result.returncode != 0:
        return set()
    return {p for p in result.stdout.split("\0") if p}


def commit_all(message: str, paths=(), new_since=None):
    """Commit the defender's fixes.

    Staged: every modified tracked file (``git add -u``), the declared
    *paths* that exist as files, and — when *new_since* is the set returned
    by :func:`untracked_files` before the defender ran — every file the
    defender created since, declared or not. Not ``git add -A``: in
    interactive mode the runner works in the operator's checkout, and ``-A``
    swept every pre-existing scratch file into a "harden round fixes" commit
    (review runner Bug 9). Each path is added on its own so one bad entry
    cannot cancel the rest, and directories are never expanded.
    """
    result = git("add", "-u")
    if result.returncode != 0:
        raise RuntimeError(f"Git add -u failed: {result.stderr.strip()}")
    to_add = [p for p in paths if Path(p).is_file()]
    if new_since is not None:
        to_add.extend(sorted(untracked_files() - set(new_since)))
    for p in to_add:
        res = git("add", "--", p)
        if res.returncode != 0:
            print(f"  Git warning: could not add {p}: {res.stderr.strip()}", file=sys.stderr)
    result = git("commit", "-m", message)
    if result.returncode != 0:
        # No changes to commit is OK
        if "nothing to commit" in result.stdout + result.stderr:
            return
        raise RuntimeError(f"Git commit failed: {result.stderr.strip()}")


# -- Claude Code helpers --

def run_claude(prompt: str, model: str | None = None) -> tuple[int, str, str]:
    return _spawn(prompt, timeout=cfg("harden", "timeout", 300), model=model)


# -- Harden script wrappers --

def _run_harden_script(payload: str, argv: list) -> tuple[bool, str]:
    """Feed *payload* to a harden validator via a temp file. (ok, output).

    ``encoding="utf-8"`` on both the temp file and the subprocess: findings
    are free text written by a model, and an arrow or an accented name in one
    of them used to raise UnicodeEncodeError under the Windows ANSI codepage
    (review runner Bug 7).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8",
    ) as f:
        f.write(payload)
        f.flush()
        result = subprocess.run(
            [sys.executable, *argv, "--file", f.name],
            capture_output=True, text=True, encoding="utf-8",
        )
    Path(f.name).unlink(missing_ok=True)
    if result.returncode == 0:
        return True, (result.stdout or "").strip()
    return False, (result.stderr or "").strip()


def validate_findings(findings_json: str, check_files: bool = False) -> tuple[bool, str]:
    """Run validate_findings.py. Returns (valid, output_or_errors)."""
    return _run_harden_script(
        findings_json, [str(VALIDATE_FINDINGS), "--sanitize"],
    )


def assign_ids(findings_json: str) -> tuple[bool, str]:
    """Run assign_ids.py --prefix ATK. Returns (success, output)."""
    return _run_harden_script(
        findings_json, [str(ASSIGN_IDS), "--prefix", "ATK"],
    )


def validate_response(response_json: str, expected_ids: list[str]) -> tuple[bool, str]:
    """Run validate_response.py. Returns (valid, output_or_errors)."""
    return _run_harden_script(
        response_json,
        [str(VALIDATE_RESPONSE), "--expected-ids", ",".join(expected_ids)],
    )


# -- Prompt builders --

def build_attacker_prompt(files: list[str], spec: dict) -> str:
    file_list = "\n".join(f"- {f}" for f in files)
    spec_text = json.dumps(spec, indent=2) if spec else "No spec available"

    return "\n".join([
        "You are an adversarial code reviewer (Attacker). Your job is to find bugs, "
        "security issues, edge cases, and spec violations in the recently changed files.",
        "",
        "## Target files",
        file_list,
        "",
        "## Spec for context",
        f"```json\n{spec_text}\n```",
        "",
        "## Instructions",
        "",
        "1. Read each target file carefully.",
        "2. Look for: bugs, security vulnerabilities, unhandled edge cases, "
        "logic errors, spec violations, input validation gaps.",
        "3. Output ONLY a JSON array of findings. Each finding:",
        '   {"category": "security|logic|input|resilience|complexity|general", '
        '"description": "what is wrong", "severity": "critical|high|medium|low", '
        '"location": "file.py:line_range"}',
        "",
        "If the code is clean and you find no issues, output an empty array: []",
        "",
        "Output ONLY the JSON array — no markdown, no explanation.",
    ])


def build_defender_prompt(findings: list[dict], files: list[str]) -> str:
    findings_text = json.dumps(findings, indent=2)
    file_list = "\n".join(f"- {f}" for f in files)

    return "\n".join([
        "You are a code defender. Your job is to fix the findings identified by "
        "the attacker. For each finding, either fix it in the code or explain why "
        "it should be deferred/rejected.",
        "",
        "## Findings to address",
        f"```json\n{findings_text}\n```",
        "",
        "## Target files",
        file_list,
        "",
        "## Instructions",
        "",
        "1. Read each target file.",
        "2. Fix each finding by editing the relevant code.",
        "3. After making fixes, output a JSON object with a 'responses' array. Each response:",
        '   {"finding_ref": "ATK-001", "action": "fixed|refactored|deferred", '
        '"description": "what you did or why you deferred"}',
        "",
        "You MUST address every finding ID. Output ONLY the JSON object:",
        '{"responses": [...]}',
    ])


# -- Main logic --

def run_attacker(files: list[str], spec: dict) -> list[dict] | None:
    """Run attacker. Returns findings list or None on failure."""
    prompt = build_attacker_prompt(files, spec)

    for attempt in range(2):
        try:
            rc, stdout, stderr = run_claude(prompt, model=cfg("harden", "attacker_model", "opus"))
        except subprocess.TimeoutExpired:
            print(f"  Attacker timed out (attempt {attempt + 1})", file=sys.stderr)
            continue
        except FileNotFoundError:
            print("Claude Code error: 'claude' command not found", file=sys.stderr)
            return None

        if rc != 0:
            print(f"  Attacker failed (attempt {attempt + 1}): {stderr.strip()[:200]}", file=sys.stderr)
            continue

        text = extract_text(stdout)
        findings_json = extract_json_from_text(text)

        # Validate
        valid, output = validate_findings(findings_json)
        if valid:
            # Assign IDs
            ok, id_output = assign_ids(output)
            if ok:
                return json.loads(id_output)
            print(f"  ID assignment failed: {id_output[:200]}", file=sys.stderr)
        else:
            print(f"  Findings validation failed (attempt {attempt + 1}): {output[:200]}", file=sys.stderr)

    return None


def run_defender(findings: list[dict], files: list[str]) -> list[dict] | None:
    """Run defender. Returns responses list or None on failure."""
    prompt = build_defender_prompt(findings, files)
    expected_ids = [f.get("id", "") for f in findings if f.get("id")]

    for attempt in range(2):
        try:
            rc, stdout, stderr = run_claude(prompt, model=cfg("harden", "defender_model", "sonnet"))
        except subprocess.TimeoutExpired:
            print(f"  Defender timed out (attempt {attempt + 1})", file=sys.stderr)
            continue
        except FileNotFoundError:
            return None

        if rc != 0:
            print(f"  Defender failed (attempt {attempt + 1}): {stderr.strip()[:200]}", file=sys.stderr)
            continue

        text = extract_text(stdout)
        response_json = extract_json_from_text(text)

        # Validate
        valid, output = validate_response(response_json, expected_ids)
        if valid:
            parsed = json.loads(output)
            return parsed.get("responses", []) if isinstance(parsed, dict) else parsed
        else:
            print(f"  Response validation failed (attempt {attempt + 1}): {output[:200]}", file=sys.stderr)

    return None


def compute_verdict(rounds: list[dict]) -> tuple[str, list[str]]:
    """Compute verdict and unresolved list from per-round data.

    Verdicts:
      - defender_failed: any round had findings but defender produced no
        responses (either None or empty list). Means the run is structurally
        broken — do NOT auto-merge; a human must inspect.
      - has_unresolved: at least one finding is not fixed/refactored.
      - all_resolved: every finding was fixed or refactored, or no findings.
    """
    defender_failed = any(
        len(rnd.get("findings", [])) > 0 and len(rnd.get("responses", [])) == 0
        for rnd in rounds
    )

    unresolved = []
    for rnd in rounds:
        for rid, action in rnd.get("resolutions", {}).items():
            if action not in ("fixed", "refactored") and rid not in unresolved:
                unresolved.append(rid)

    if defender_failed:
        return "defender_failed", unresolved
    if unresolved:
        return "has_unresolved", unresolved
    return "all_resolved", unresolved


def main():
    parser = argparse.ArgumentParser(description="Autonomous hardener — runner stage 5")
    parser.add_argument("execution_log", help="Path to execution log JSON")
    args = parser.parse_args()

    # Read execution log
    log_path = Path(args.execution_log)
    if not log_path.exists():
        print(f"Execution log not found: {args.execution_log}", file=sys.stderr)
        sys.exit(1)

    try:
        execution = json.loads(log_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid execution log JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        assert_schema_version(execution, "execution", EXECUTION_SCHEMA_VERSION)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if execution.get("status") != "completed":
        print("Cannot harden aborted execution", file=sys.stderr)
        sys.exit(1)

    uuid = execution.get("uuid")
    branch = execution.get("branch", f"runner/{uuid}")

    # Collect changed files (skip steps with no files)
    changed_files = iter_unique(
        f for step in execution.get("steps", [])
        for f in step.get("files_changed", [])
    )

    if not changed_files:
        print("No changed files to harden", file=sys.stderr)
        # Write clean result
        HARDENS_DIR.mkdir(parents=True, exist_ok=True)
        result = {
            "schema_version": HARDEN_SCHEMA_VERSION,
            "uuid": uuid, "branch": branch, "verdict": "all_resolved",
            "rounds": [], "unresolved": [], "total_findings": 0, "total_resolved": 0,
        }
        result_path = HARDENS_DIR / f"{uuid}.json"
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"all_resolved\n{result_path}")
        sys.exit(0)

    # Check branch exists
    if not branch_exists(branch):
        print(f"Branch {branch} not found", file=sys.stderr)
        sys.exit(1)

    # Read spec from plan if available
    plan_path = plans_dir() / f"{uuid}.json"
    spec = {}
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            assert_schema_version(plan, "plan", PLAN_SCHEMA_VERSION)
            spec = plan.get("spec", {})
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Checkout branch
    original_branch = get_current_branch()
    try:
        checkout(branch)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    HARDENS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = HARDENS_DIR / f"{uuid}.json"

    rounds = []
    all_findings_by_id = {}
    total_findings = 0
    total_resolved = 0

    try:
        for round_num in range(1, MAX_ROUNDS + 1):
            print(f"Harden round {round_num}/{MAX_ROUNDS}...", file=sys.stderr)

            # Attack
            findings = run_attacker(changed_files, spec)
            if findings is None:
                print(f"  Attacker failed in round {round_num}, aborting harden", file=sys.stderr)
                checkout(original_branch)
                sys.exit(1)

            if len(findings) == 0:
                # Clean — no issues found
                rounds.append({
                    "round": round_num,
                    "findings": [],
                    "responses": [],
                    "resolutions": {},
                })
                print(f"  Round {round_num}: attacker found 0 issues — clean", file=sys.stderr)
                break

            total_findings += len(findings)
            for f in findings:
                fid = f.get("id", "")
                if fid:
                    all_findings_by_id[fid] = f

            print(f"  Round {round_num}: attacker found {len(findings)} issue(s)", file=sys.stderr)

            # Defend — snapshot untracked files first so the commit below can
            # include what the defender creates without sweeping what the
            # operator already had lying around.
            untracked_before = untracked_files()
            responses = run_defender(findings, changed_files)
            if responses is None or len(responses) == 0:
                # Defender produced no responses despite findings — mark all unresolved.
                # Covers two failure modes: run_defender returned None (subprocess/validation
                # error) and run_defender returned [] (validation passed on empty array).
                resolutions = {f.get("id", f"unknown-{i}"): "unresolved" for i, f in enumerate(findings)}
                rounds.append({
                    "round": round_num,
                    "findings": findings,
                    "responses": [],
                    "resolutions": resolutions,
                })
                print(f"  Defender produced no responses in round {round_num}, findings marked unresolved", file=sys.stderr)
                break

            # Commit defender fixes
            try:
                commit_all(f"runner/{uuid} harden round {round_num} fixes", changed_files,
                           new_since=untracked_before)
            except RuntimeError as e:
                print(f"  Git warning: {e}", file=sys.stderr)

            # Build resolutions
            resolutions = {}
            for resp in responses:
                rid = resp.get("finding_ref", "")
                action = resp.get("action", "unresolved")
                resolutions[rid] = action
                if action in ("fixed", "refactored"):
                    total_resolved += 1

            rounds.append({
                "round": round_num,
                "findings": findings,
                "responses": responses,
                "resolutions": resolutions,
            })

            # Check if all resolved
            unresolved_this_round = [rid for rid, act in resolutions.items() if act not in ("fixed", "refactored")]
            if not unresolved_this_round:
                print(f"  Round {round_num}: all findings resolved", file=sys.stderr)
                # Continue to next round to verify the fixes are clean
                continue

            print(f"  Round {round_num}: {len(unresolved_this_round)} unresolved", file=sys.stderr)

    finally:
        # Always return to original branch
        checkout(original_branch)

    verdict, unresolved = compute_verdict(rounds)

    harden_result = {
        "schema_version": HARDEN_SCHEMA_VERSION,
        "uuid": uuid,
        "branch": branch,
        "verdict": verdict,
        "rounds": rounds,
        "unresolved": unresolved,
        "total_findings": total_findings,
        "total_resolved": total_resolved,
    }

    result_path.write_text(json.dumps(harden_result, indent=2), encoding="utf-8")
    print(f"{verdict}\n{result_path}")


if __name__ == "__main__":
    main()
