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

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
HARDENS_DIR = SCRIPT_DIR / "hardens"
HARDEN_DIR = REPO_DIR / "harden"

VALIDATE_FINDINGS = HARDEN_DIR / "validate_findings.py"
VALIDATE_RESPONSE = HARDEN_DIR / "validate_response.py"
ASSIGN_IDS = HARDEN_DIR / "assign_ids.py"

MAX_ROUNDS = cfg("harden", "max_rounds", 3)


# -- Git helpers (#253: shared via runner/git_lib.py) --

from .git_lib import git


def branch_exists(branch: str) -> bool:
    result = git("rev-parse", "--verify", branch)
    return result.returncode == 0


def checkout(branch: str):
    result = git("checkout", branch)
    if result.returncode != 0:
        raise RuntimeError(f"Git checkout failed: {result.stderr.strip()}")


def commit_all(message: str):
    git("add", "-A")
    result = git("commit", "-m", message)
    if result.returncode != 0:
        # No changes to commit is OK
        if "nothing to commit" in result.stdout + result.stderr:
            return
        raise RuntimeError(f"Git commit failed: {result.stderr.strip()}")


def get_current_branch() -> str:
    result = git("rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


# -- Claude Code helpers --

def run_claude(prompt: str, model: str | None = None) -> tuple[int, str, str]:
    from claude_subprocess import run_claude as _spawn
    return _spawn(prompt, timeout=cfg("harden", "timeout", 300), model=model)


def extract_text(raw_output: str) -> str:
    """Extract text content from Claude Code JSON envelope."""
    try:
        envelope = json.loads(raw_output)
        if isinstance(envelope, dict) and "result" in envelope:
            return envelope["result"]
    except (json.JSONDecodeError, TypeError):
        pass
    return raw_output


def extract_json_from_text(text: str) -> str:
    """Extract JSON array/object from text that may contain markdown fences or prose.

    Uses JSONDecoder.raw_decode to find the first complete JSON value, which
    tolerates trailing content (prose, additional JSON blocks, explanation, etc.).
    """
    text = text.strip()

    # Try to extract from markdown code fences first
    import re
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Use raw_decode to parse the first complete JSON value, ignoring trailing data.
    # Scan forward from each candidate '{' or '[' until raw_decode succeeds.
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, end = decoder.raw_decode(text, i)
                # Re-serialize to canonical form (drops trailing prose/JSON)
                return json.dumps(obj)
            except json.JSONDecodeError:
                continue

    return text


# -- Harden script wrappers --

def validate_findings(findings_json: str, check_files: bool = False) -> tuple[bool, str]:
    """Run validate_findings.py. Returns (valid, output_or_errors)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(findings_json)
        f.flush()
        result = subprocess.run(
            [sys.executable, str(VALIDATE_FINDINGS), "--file", f.name, "--sanitize"],
            capture_output=True, text=True,
        )
    Path(f.name).unlink(missing_ok=True)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, result.stderr.strip()


def assign_ids(findings_json: str) -> tuple[bool, str]:
    """Run assign_ids.py --prefix ATK. Returns (success, output)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(findings_json)
        f.flush()
        result = subprocess.run(
            [sys.executable, str(ASSIGN_IDS), "--prefix", "ATK", "--file", f.name],
            capture_output=True, text=True,
        )
    Path(f.name).unlink(missing_ok=True)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, result.stderr.strip()


def validate_response(response_json: str, expected_ids: list[str]) -> tuple[bool, str]:
    """Run validate_response.py. Returns (valid, output_or_errors)."""
    ids_str = ",".join(expected_ids)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(response_json)
        f.flush()
        result = subprocess.run(
            [sys.executable, str(VALIDATE_RESPONSE), "--expected-ids", ids_str, "--file", f.name],
            capture_output=True, text=True,
        )
    Path(f.name).unlink(missing_ok=True)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, result.stderr.strip()


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

        # Debug: log what we got back
        print(f"  DEBUG defender stdout len={len(stdout)}, text len={len(text)}, json len={len(response_json)}", file=sys.stderr)
        print(f"  DEBUG response_json first 500: {response_json[:500]!r}", file=sys.stderr)

        # Validate
        valid, output = validate_response(response_json, expected_ids)
        if valid:
            parsed = json.loads(output)
            return parsed.get("responses", []) if isinstance(parsed, dict) else parsed
        else:
            print(f"  Response validation failed (attempt {attempt + 1}): {output[:200]}", file=sys.stderr)

    return None


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

    if execution.get("status") != "completed":
        print("Cannot harden aborted execution", file=sys.stderr)
        sys.exit(1)

    uuid = execution.get("uuid")
    branch = execution.get("branch", f"runner/{uuid}")

    # Collect changed files (skip steps with no files)
    changed_files = []
    for step in execution.get("steps", []):
        for f in step.get("files_changed", []):
            if f and f not in changed_files:
                changed_files.append(f)

    if not changed_files:
        print("No changed files to harden", file=sys.stderr)
        # Write clean result
        HARDENS_DIR.mkdir(parents=True, exist_ok=True)
        result = {
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
    plan_path = SCRIPT_DIR / "plans" / f"{uuid}.json"
    spec = {}
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            spec = plan.get("spec", {})
        except (json.JSONDecodeError, KeyError):
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

            # Defend
            responses = run_defender(findings, changed_files)
            if responses is None:
                # Defender failed — mark all findings as unresolved
                resolutions = {f.get("id", f"unknown-{i}"): "unresolved" for i, f in enumerate(findings)}
                rounds.append({
                    "round": round_num,
                    "findings": findings,
                    "responses": [],
                    "resolutions": resolutions,
                })
                print(f"  Defender failed in round {round_num}, findings marked unresolved", file=sys.stderr)
                break

            # Commit defender fixes
            try:
                commit_all(f"runner/{uuid} harden round {round_num} fixes")
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

    # Determine verdict
    unresolved = []
    for rnd in rounds:
        for rid, action in rnd.get("resolutions", {}).items():
            if action not in ("fixed", "refactored") and rid not in unresolved:
                unresolved.append(rid)

    verdict = "all_resolved" if not unresolved else "has_unresolved"

    harden_result = {
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
