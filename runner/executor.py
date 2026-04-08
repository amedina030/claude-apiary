#!/usr/bin/env python3
"""
Executor — Stage 4 of the runner.

Reads a validated plan JSON, creates a feature branch, and executes each step
sequentially via individual Claude Code subprocess calls. Commits after each
step and tracks results in an execution log.

Output:
  - Git branch runner/<uuid> with one commit per completed step
  - runner/executions/<uuid>.json execution log

Usage:
    executor.py <path_to_plan.json>
"""
import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

from config_loader import get as cfg
# Eager-import claude_subprocess (and transitively cost_emit) at module top.
# These modules MUST be resolved while the working tree is still on the
# parent branch (typically master). If we let run_claude() do a deferred
# import, Python loads the source AFTER executor.main() has done
# `git checkout runner/<uuid>`, which silently picks up whatever older
# copies happen to live on the runner branch and shadows every fix we
# ship on master. Loading them now caches the master versions in
# sys.modules so all later calls use them regardless of working-tree state.
from claude_subprocess import run_claude as _spawn_claude

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
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


def _path_is_outside_repo(file_str: str) -> bool:
    """True if `file_str` resolves to a path outside the repo working tree.

    Used by commit_or_verify_files() to split the file list into in-repo
    files (verified via git) and out-of-repo files (verified via content
    hash). #212.
    """
    try:
        resolved = Path(file_str).resolve()
        resolved.relative_to(REPO_ROOT.resolve())
        return False
    except (ValueError, OSError, RuntimeError):
        return True


def _hash_file(path: Path) -> str:
    """Return sha256 hex of `path`'s contents, or empty string if missing.

    Empty-string sentinel is intentional: a file that didn't exist before
    a step but exists after still counts as 'changed' (empty != non-empty
    digest), which is exactly what we want.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return ""


def hash_outside_files(files: list[str]) -> dict[str, str]:
    """Snapshot content hashes of out-of-repo files in `files`.

    Returns {file_str: sha256_hex}, with empty string for missing files.
    Caller passes the result to commit_or_verify_files() after the
    subprocess runs to verify at least one out-of-repo file changed.
    """
    out: dict[str, str] = {}
    for f in files:
        if not isinstance(f, str) or not f:
            continue
        if _path_is_outside_repo(f):
            out[f] = _hash_file(Path(f))
    return out


def commit_or_verify_files(
    files: list[str],
    message: str,
    prior_outside_hashes: dict[str, str] | None = None,
) -> None:
    """Verify expected file changes and commit the in-repo subset (#212).

    Before committing, verify the subprocess actually changed the files
    it claimed to. Splits `files` into two buckets:

    * **in-repo**: staged with `git add` and verified via
      `git diff --cached --quiet`. If nothing is staged, the subprocess
      either no-op'd, wrote to the wrong path, or silently failed.

    * **outside-repo**: verified by re-hashing each path and comparing
      to the snapshot in `prior_outside_hashes`. At least one hash must
      differ. Outside files are NOT staged or committed (they live
      outside the worktree).

    A commit is created only if there is at least one in-repo file with
    a real diff. If every file in the step is outside the repo, the
    function still verifies them and returns without touching git.
    """
    if not files:
        return
    prior_outside_hashes = prior_outside_hashes or {}

    in_repo = [f for f in files if not _path_is_outside_repo(f)]
    outside = [f for f in files if _path_is_outside_repo(f)]

    # Verify outside files via content hash diff.
    if outside:
        unchanged_outside = []
        for f in outside:
            current = _hash_file(Path(f))
            if current == prior_outside_hashes.get(f, ""):
                unchanged_outside.append(f)
        if len(unchanged_outside) == len(outside):
            raise RuntimeError(
                f"Subprocess made no changes to expected files ({', '.join(outside)}). "
                f"All listed out-of-repo paths have the same content hash they had "
                f"before the step ran — the implementation subprocess either decided "
                f"no edit was needed, wrote to the wrong path, or silently failed."
            )

    # Verify and commit in-repo files via git.
    if in_repo:
        git("add", *in_repo)
        staged = git("diff", "--cached", "--quiet", "--", *in_repo)
        if staged.returncode == 0:
            # `git diff --cached --quiet` exits 0 when there is no diff,
            # meaning nothing was actually staged for these files.
            raise RuntimeError(
                f"Subprocess made no changes to expected files ({', '.join(in_repo)}). "
                f"The step's implementation subprocess either decided no edit was "
                f"needed, edited a different path, or silently failed. Check the "
                f"subprocess transcript."
            )
        result = git("commit", "-m", message)
        if result.returncode != 0:
            raise RuntimeError(_format_git_error(
                "committing",
                result,
                extra=f"staged files: {', '.join(in_repo)}",
            ))


# Backward-compat alias for any callers that imported the old name.
commit_files = commit_or_verify_files


def get_current_branch() -> str:
    result = git("rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def load_previous_log(log_path: Path) -> dict:
    """Read a prior execution log and return {step_number: entry} or {}.

    Used on resume so we can preserve the per-step status from previous
    runs (especially verify/test steps, which don't commit and therefore
    can't be recovered from git log alone). Returns an empty dict if the
    file is missing or unreadable — callers treat that as "no prior data"
    and fall back to fresh entries.
    """
    if not log_path.exists():
        return {}
    try:
        prior = json.loads(log_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    by_num = {}
    for entry in prior.get("steps", []):
        if isinstance(entry, dict) and isinstance(entry.get("step_number"), int):
            by_num[entry["step_number"]] = entry
    return by_num


def get_completed_step_numbers(uuid: str) -> set:
    """Return the set of step numbers already committed on the current branch.

    Executor's commit messages follow the pattern:
        runner/<uuid> step <N>: <description>
    Parses git log on HEAD for matching subjects. Used on resume so the
    executor skips steps that were committed in a previous run rather than
    re-running them (which collides with the "nothing to commit, working
    tree clean" failure mode when Claude correctly makes no changes to an
    already-updated file).

    Only steps that produced a commit are detected — verify/test steps
    don't commit and will re-run on resume, which is fine because they
    are idempotent checks.
    """
    result = git("log", "--format=%s", "HEAD")
    if result.returncode != 0:
        return set()
    prefix = f"runner/{uuid} step "
    completed = set()
    for line in result.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):]
        num_str = rest.split(":", 1)[0].strip()
        try:
            completed.add(int(num_str))
        except ValueError:
            continue
    return completed


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
    return _spawn_claude(prompt, timeout=cfg("executor", "timeout", 300), model=model)


def run_test_command(code_spec: str) -> tuple[bool, str]:
    """Execute a test command from code_spec. Returns (passed, output)."""
    command = code_spec.strip()
    if not command:
        return False, 'No test command in code_spec'
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return False, f'could not parse test command: {e}'
    if not argv:
        return False, 'No test command in code_spec'
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as e:
        return False, f'test command not found: {argv[0]} — code_spec must be a single command starting with an executable on PATH ({e})'
    output = (result.stdout or '') + (result.stderr or '')
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
                    # Store full output (no truncation) — test failures need
                    # complete tracebacks for diagnosis. The execution log is
                    # JSON, not console output, so length is not a concern.
                    result["error"] = f"Test failed (attempt {attempt}): {output}"
                    # Test files don't change between attempts in the same
                    # executor run, so retrying is guaranteed to waste a slot.
                    # Bail out of the retry loop after the first failure.
                    break

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
    parser = argparse.ArgumentParser(description="Executor — runner stage 4")
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
    branch = f"runner/{uuid}"

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

    # Detect steps already committed on the branch (resume path). Git is
    # authoritative because it is the actual record of what landed; the
    # execution log is derivative and gets overwritten on each run.
    completed_step_numbers = get_completed_step_numbers(uuid)
    if completed_step_numbers:
        print(
            f"Resume: skipping {len(completed_step_numbers)} already-committed "
            f"step(s): {sorted(completed_step_numbers)}",
            file=sys.stderr,
        )

    # Execute
    EXECUTIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = EXECUTIONS_DIR / f"{uuid}.json"

    # Read prior execution log (if any) BEFORE we start writing the new one.
    # The map lets us preserve verify/test step status from previous runs,
    # which git log can't recover because verify/test steps don't commit.
    previous_entries = load_previous_log(log_path)

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

        # Resume: carry forward already-committed steps as passed without
        # re-running them. Their commit already exists on the branch, so
        # re-running would produce "nothing to commit" and abort the runner.
        # If the previous execution log has a richer entry for this step
        # (real files_changed list, original timing, etc.), prefer that
        # over the bland stub. Otherwise fall back to the stub.
        if step_num in completed_step_numbers:
            prior = previous_entries.get(step_num)
            if isinstance(prior, dict) and prior.get("status") == "passed":
                carried = dict(prior)
                carried["note"] = "carried forward from previous run (commit on branch)"
                execution_log["steps"].append(carried)
            else:
                execution_log["steps"].append({
                    "step_number": step_num,
                    "status": "passed",
                    "files_changed": step.get("files", []),
                    "error": None,
                    "note": "carried forward from previous run (commit on branch)",
                })
            continue

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

        # Snapshot any out-of-repo files BEFORE the subprocess runs so
        # commit_or_verify_files() can detect that they actually changed
        # (the git-based check can't see paths outside the worktree). #212.
        prior_outside_hashes = hash_outside_files(step.get("files", []))

        step_result = execute_step(step, spec, resolved_model)
        execution_log["steps"].append(step_result)

        if step_result["status"] == "passed":
            # Commit if there are files
            files = step.get("files", [])
            if files and step.get("action") not in ("test", "verify"):
                try:
                    commit_or_verify_files(
                        files,
                        f"runner/{uuid} step {step_num}: {step.get('description', '')}",
                        prior_outside_hashes,
                    )
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
                            "error": "Runner aborted",
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
                        "error": "Runner aborted",
                    })
            break

    if aborted:
        execution_log["status"] = "aborted"

    # Write execution log
    log_path.write_text(json.dumps(execution_log, indent=2), encoding="utf-8")

    if aborted:
        print(f"Runner aborted. Log: {log_path}", file=sys.stderr)
        # Return to original branch
        git("checkout", original_branch)
        sys.exit(1)

    print(f"Branch: {branch}")
    print(f"Log: {log_path}")


if __name__ == "__main__":
    main()
