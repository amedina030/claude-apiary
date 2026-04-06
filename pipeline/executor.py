#!/usr/bin/env python3
"""
Executor — Stage 4 of the pipeline.

Reads a validated plan JSON, creates a feature branch, and executes each step
sequentially via individual Claude Code subprocess calls. Commits after each
step and tracks results in an execution log.

Output:
  - Git branch pipeline/<uuid> with one commit per completed step
  - pipeline/executions/<uuid>.json execution log

Usage:
    executor.py <path_to_plan.json>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
EXECUTIONS_DIR = SCRIPT_DIR / "executions"

MAX_STEP_RETRIES = cfg("executor", "max_retries_per_step", 2)


# -- Git helpers --

def git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True,
    )


def branch_exists(branch: str) -> bool:
    # ATK-006: check refs/heads/ explicitly so remote tracking refs don't match
    result = git("rev-parse", "--verify", f"refs/heads/{branch}")
    return result.returncode == 0


def _format_git_error(action: str, result: subprocess.CompletedProcess, extra: str = "") -> str:
    """Build a RuntimeError message that includes both stdout and stderr.

    Git often writes failure context (e.g. 'nothing to commit') to stdout, not
    stderr, so surfacing only stderr leaves operators with empty error messages.
    """
    parts = [f"Git error {action} (exit {result.returncode})"]
    if extra:
        parts.append(extra)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        parts.append(f"stdout: {stdout}")
    if stderr:
        parts.append(f"stderr: {stderr}")
    if not stdout and not stderr:
        parts.append("(no output captured)")
    return "\n".join(parts)


def create_branch(branch: str):
    result = git("checkout", "-b", branch)
    if result.returncode != 0:
        raise RuntimeError(_format_git_error(f"creating branch '{branch}'", result))


def commit_files(files: list[str], message: str):
    """Stage specific files and commit."""
    if not files:
        return
    git("add", *files)
    result = git("commit", "-m", message)
    if result.returncode != 0:
        raise RuntimeError(_format_git_error(
            "committing",
            result,
            extra=f"staged files: {', '.join(files)}",
        ))


def get_current_branch() -> str:
    result = git("rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


# -- Topological sort --

def topo_sort(steps: list[dict]) -> list[dict]:
    """Sort steps by dependency order (topological sort)."""
    by_num = {s["step_number"]: s for s in steps}
    visited = set()
    order = []

    def visit(num):
        if num in visited:
            return
        visited.add(num)
        step = by_num.get(num)
        if step:
            for dep in step.get("depends_on", []):
                if dep in by_num:
                    visit(dep)
            order.append(step)

    for num in sorted(by_num.keys()):
        visit(num)

    return order


# -- Step execution --

def build_step_prompt(step: dict, spec: dict) -> str:
    """Build the prompt for a create/modify/delete step."""
    parts = [
        f"You are implementing step {step['step_number']} of a plan.",
        "",
        f"## Step: {step['description']}",
        f"**Type:** {step['type']}",
        f"**Action:** {step['action']}",
        f"**Files:** {', '.join(step.get('files', []))}",
        "",
        "## Code specification",
        "",
        step.get("code_spec", ""),
        "",
        "## Instructions",
        "",
    ]

    action = step.get("action", "")
    if action == "create":
        parts.append("Create the file(s) listed above with the implementation described in the code specification.")
    elif action == "modify":
        parts.append("Read the existing file(s) listed above and apply the changes described in the code specification.")
    elif action == "delete":
        parts.append("Delete the file(s) listed above as described in the code specification.")

    parts.extend([
        "",
        "Write the actual code — not pseudocode, not explanations. Just implement it.",
        "Use the existing codebase patterns and conventions.",
    ])

    return "\n".join(parts)


def build_verify_prompt(step: dict, spec: dict) -> str:
    """Build the prompt for a verify step."""
    return "\n".join([
        f"You are verifying step {step['step_number']} of a plan.",
        "",
        f"## Verification: {step['description']}",
        "",
        f"## What to check",
        step.get("code_spec", ""),
        "",
        "## Instructions",
        "",
        "Read the relevant files and confirm whether the acceptance criterion is met.",
        "Output ONLY a JSON object: {\"passed\": true/false, \"explanation\": \"brief reason\"}",
    ])


def run_claude(prompt: str, model: str) -> tuple[int, str, str]:
    """Run Claude Code subprocess with the specified model."""
    from claude_subprocess import run_claude as _spawn
    return _spawn(prompt, timeout=cfg("executor", "timeout", 300), model=model)


def run_test_command(code_spec: str) -> tuple[bool, str]:
    """Execute a test command from code_spec. Returns (passed, output)."""
    # The code_spec for test steps contains the command to run
    command = code_spec.strip()
    if not command:
        return False, "No test command in code_spec"

    result = subprocess.run(
        command, shell=True,
        capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    return result.returncode == 0, output.strip()


def execute_step(step: dict, spec: dict, model: str) -> dict:
    """Execute a single step. Returns a step result dict."""
    step_num = step["step_number"]
    action = step.get("action", "")
    result = {
        "step_number": step_num,
        "status": "failed",
        "files_changed": step.get("files", []),
        "error": None,
    }

    for attempt in range(1, MAX_STEP_RETRIES + 1):
        try:
            if action == "test":
                passed, output = run_test_command(step.get("code_spec", ""))
                if passed:
                    result["status"] = "passed"
                    result["error"] = None
                    return result
                else:
                    result["error"] = f"Test failed (attempt {attempt}): {output[:500]}"

            elif action == "verify":
                prompt = build_verify_prompt(step, spec)
                rc, stdout, stderr = run_claude(prompt, model)
                if rc != 0:
                    result["error"] = f"Claude Code error (attempt {attempt}): {stderr.strip()[:500]}"
                    # -1 = subprocess timeout, -2 = binary not found / permission denied.
                    # Both are deterministic failures — retrying with identical settings
                    # is guaranteed to waste tokens, so abort the retry loop now.
                    if rc in (-1, -2):
                        break
                    continue

                # Parse verify result — handle envelope, code fences, and prose
                verify = {"passed": False, "explanation": "Unparseable output"}
                try:
                    envelope = json.loads(stdout)
                    if isinstance(envelope, dict) and "result" in envelope:
                        text = envelope["result"].strip()
                    elif isinstance(envelope, dict) and "passed" in envelope:
                        verify = envelope
                        text = None
                    else:
                        text = stdout.strip()

                    if text is not None:
                        # Strip markdown code fences if present
                        if text.startswith("```"):
                            lines = text.splitlines()
                            if lines[-1].strip() == "```":
                                lines = lines[1:-1]
                            else:
                                lines = lines[1:]
                            text = "\n".join(lines).strip()
                        # Find first { and last } to extract JSON from prose
                        start = text.find("{")
                        end = text.rfind("}")
                        if start != -1 and end > start:
                            try:
                                verify = json.loads(text[start:end + 1])
                            except json.JSONDecodeError:
                                pass
                except (json.JSONDecodeError, TypeError):
                    pass

                if verify.get("passed"):
                    result["status"] = "passed"
                    result["error"] = None
                    return result
                else:
                    result["error"] = f"Verify failed (attempt {attempt}): {verify.get('explanation', 'unknown')}"

            else:
                # create/modify/delete
                prompt = build_step_prompt(step, spec)
                rc, stdout, stderr = run_claude(prompt, model)
                if rc != 0:
                    result["error"] = f"Claude Code error (attempt {attempt}): {stderr.strip()[:500]}"
                    # -1 = subprocess timeout, -2 = binary not found / permission denied.
                    # Both are deterministic failures — retrying with identical settings
                    # is guaranteed to waste tokens, so abort the retry loop now.
                    if rc in (-1, -2):
                        break
                    continue

                # Success — Claude wrote the files
                result["status"] = "passed"
                result["error"] = None
                return result

        except subprocess.TimeoutExpired:
            result["error"] = f"Subprocess timed out (attempt {attempt})"
        except FileNotFoundError:
            result["error"] = "'claude' command not found"
            return result  # No point retrying

    return result


def main():
    parser = argparse.ArgumentParser(description="Executor — pipeline stage 4")
    parser.add_argument("plan", help="Path to plan JSON file")
    args = parser.parse_args()

    # Read plan
    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"Plan file not found: {args.plan}", file=sys.stderr)
        sys.exit(1)

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid plan JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not plan.get("valid", False):
        print("Plan is not valid -- cannot execute invalid plan", file=sys.stderr)
        sys.exit(1)

    uuid = plan.get("uuid", plan_path.stem)

    # ATK-005: validate uuid from plan JSON to prevent path traversal
    if not isinstance(uuid, str):
        print("Plan uuid field is not a string", file=sys.stderr)
        sys.exit(1)
    uuid = uuid.strip()
    if (
        not uuid
        or "\\" in uuid
        or "\x00" in uuid
        or uuid in (".", "..")
        or Path(uuid) != Path(Path(uuid).name)
        or not Path(uuid).name
    ):
        print("Plan uuid field contains invalid characters (path separators not allowed)", file=sys.stderr)
        sys.exit(1)

    default_model = plan.get("executor_model", "sonnet")
    spec = plan.get("spec", {})
    steps = plan.get("steps", [])
    branch = f"pipeline/{uuid}"

    # Remember original branch to return to on error
    original_branch = get_current_branch()

    # Check out existing branch or create new one
    if branch_exists(branch):
        print(f"Branch {branch} already exists, checking out", file=sys.stderr)
        result = git("checkout", branch)
        if result.returncode != 0:
            print(_format_git_error(f"checking out branch '{branch}'", result), file=sys.stderr)
            sys.exit(1)
    else:
        try:
            create_branch(branch)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    # Sort steps by dependency order
    sorted_steps = topo_sort(steps)

    # Execute
    EXECUTIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = EXECUTIONS_DIR / f"{uuid}.json"
    execution_log = {
        "uuid": uuid,
        "branch": branch,
        "status": "completed",
        "steps": [],
    }

    failed_steps = set()
    aborted = False

    for step in sorted_steps:
        step_num = step["step_number"]
        deps = step.get("depends_on", [])

        # Skip if any dependency failed/skipped
        if any(d in failed_steps for d in deps):
            step_result = {
                "step_number": step_num,
                "status": "skipped",
                "files_changed": step.get("files", []),
                "error": "Dependency failed or was skipped",
            }
            execution_log["steps"].append(step_result)
            failed_steps.add(step_num)
            continue

        print(f"Executing step {step_num}: {step.get('description', '')}", file=sys.stderr)
        resolved_model = step.get("model") or default_model
        step_result = execute_step(step, spec, resolved_model)
        execution_log["steps"].append(step_result)

        if step_result["status"] == "passed":
            # Commit if there are files
            files = step.get("files", [])
            if files and step.get("action") not in ("test", "verify"):
                try:
                    commit_files(files, f"pipeline/{uuid} step {step_num}: {step.get('description', '')}")
                except RuntimeError as e:
                    print(f"Git error: {e}", file=sys.stderr)
                    step_result["status"] = "failed"
                    step_result["error"] = str(e)
                    failed_steps.add(step_num)
                    aborted = True
                    # Mark remaining as skipped
                    remaining = [s for s in sorted_steps if s["step_number"] > step_num]
                    for r in remaining:
                        execution_log["steps"].append({
                            "step_number": r["step_number"],
                            "status": "skipped",
                            "files_changed": r.get("files", []),
                            "error": "Pipeline aborted",
                        })
                    break
        else:
            failed_steps.add(step_num)
            aborted = True
            # Mark remaining as skipped
            remaining_nums = {s["step_number"] for s in sorted_steps}
            logged_nums = {s["step_number"] for s in execution_log["steps"]}
            for s in sorted_steps:
                if s["step_number"] not in logged_nums:
                    execution_log["steps"].append({
                        "step_number": s["step_number"],
                        "status": "skipped",
                        "files_changed": s.get("files", []),
                        "error": "Pipeline aborted",
                    })
            break

    if aborted:
        execution_log["status"] = "aborted"

    # Write execution log
    log_path.write_text(json.dumps(execution_log, indent=2), encoding="utf-8")

    if aborted:
        print(f"Pipeline aborted. Log: {log_path}", file=sys.stderr)
        # Return to original branch
        git("checkout", original_branch)
        sys.exit(1)

    print(f"Branch: {branch}")
    print(f"Log: {log_path}")


if __name__ == "__main__":
    main()
