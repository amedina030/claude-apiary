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
import json
import os
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
EXECUTIONS_DIR = SCRIPT_DIR / "executions"

MAX_STEP_RETRIES = cfg("executor", "max_retries_per_step", 2)


# -- Git helpers (#253: shared via runner/git_lib.py) --

from git_lib import git, format_git_error as _format_git_error


def branch_exists(branch: str) -> bool:
    # ATK-006: check refs/heads/ explicitly so remote tracking refs don't match
    result = git("rev-parse", "--verify", f"refs/heads/{branch}")
    return result.returncode == 0


def create_branch(branch: str):
    result = git("checkout", "-b", branch)
    if result.returncode != 0:
        raise RuntimeError(_format_git_error(f"creating branch '{branch}'", result))


def assert_files_clean(files: list[str]):
    """Abort if any of `files` has uncommitted changes (#235).

    Without this pre-check, a pre-existing dirty worktree pollutes the
    runner: if the user has local edits to a file the step targets, the
    subsequent `git add` stages those edits, `git diff --cached` reports
    a real diff, verification passes even though the subprocess did
    nothing, and the runner commits the user's work under a
    'runner/<uuid> step N' message — stealing the changes and making
    them look like the runner authored them.

    We check `git diff HEAD -- <files>` which covers both staged and
    unstaged differences vs. the committed state. Non-existent files
    (e.g. for create actions) produce no diff, so they pass cleanly.
    """
    if not files:
        return
    result = git("diff", "HEAD", "--", *files)
    if result.returncode != 0:
        # Diff failure itself shouldn't block the runner — fall through
        # and let the existing "no changes" check handle weirdness.
        return
    if result.stdout.strip():
        raise RuntimeError(
            f"Refusing to run step: uncommitted changes exist in target "
            f"files ({', '.join(files)}). The runner cannot safely edit "
            f"these without commingling your local work into its commit. "
            f"Commit or stash your changes before re-running."
        )


def snapshot_worktree_state() -> set:
    """Capture the full set of porcelain-v1 status lines for the worktree.

    Used as the baseline for assert_no_unexpected_writes (#236). Includes
    untracked files (--untracked-files=all) so the runner catches a
    subprocess that writes a brand-new file outside step.files. Returns
    an empty set on any git failure — the caller treats that as "no
    baseline" and the post-step check becomes a no-op rather than
    crashing the runner.
    """
    result = git("status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        return set()
    return {line for line in result.stdout.splitlines() if line.strip()}


def _porcelain_path(line: str) -> str:
    """Extract the file path from a `git status --porcelain=v1` line.

    Format: 'XY path' where XY is a two-character status code. Rename
    and copy records look like 'R  orig -> new' / 'C  orig -> new'; we
    return the destination path (the one that actually got written).
    """
    if len(line) < 4:
        return ""
    rest = line[3:]
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1]
    return rest.strip().strip('"')


def _norm_rel(path: str) -> str:
    """Normalize a repo-relative path for comparison: forward slashes,
    no leading './'. Preserves case (we rely on normcase elsewhere only
    where we know we're on Windows; porcelain output is already in the
    on-disk case)."""
    p = path.replace("\\", "/").lstrip("./")
    return p.strip("/")


def assert_no_unexpected_writes(
    pre: set, post: set, expected_files: list,
):
    """Raise if the step wrote to any path not listed in step.files (#236).

    Set-difference on porcelain lines so that a file that was already
    dirty before the step (and is still dirty afterward with the same
    status code) does NOT show up. Paths are normalized to forward
    slashes for comparison against step.files.

    The verifier previously only checked that EXPECTED files changed —
    a subprocess writing garbage to an unrelated path (README.md,
    core/session.py, etc.) was invisible and shipped. This closes that
    gap.
    """
    new_lines = post - pre
    if not new_lines:
        return
    expected = {_norm_rel(f) for f in expected_files if isinstance(f, str)}
    unexpected = set()
    for line in new_lines:
        path = _porcelain_path(line)
        if not path:
            continue
        if _norm_rel(path) in expected:
            continue
        unexpected.add(path)
    if unexpected:
        raise RuntimeError(
            f"Step wrote to unexpected path(s) not declared in step.files: "
            f"{', '.join(sorted(unexpected))}. The runner only permits "
            f"writes to files the step explicitly declared — if this "
            f"write is intentional, add the path to step.files in the "
            f"plan; otherwise the subprocess is misbehaving and the "
            f"change would have shipped uncaught."
        )


_ACTION_TO_STATUS_CODES = {
    "create": {"A"},
    "modify": {"M"},
    "delete": {"D"},
}


def _assert_action_matches_staged(action: str, files: list):
    """Cross-check `git diff --cached --name-status` against the step's
    declared action (#237).

    Without this, a 'modify' action whose subprocess accidentally DELETES
    the file passes verification: `git add` stages the deletion, the
    generic staged-diff check reports a diff, and commit_files calls it
    good. The action/operation mismatch is silently committed. This
    helper compares each staged path's status code (A/M/D) against the
    codes allowed for the declared action.
    """
    if action not in _ACTION_TO_STATUS_CODES:
        return
    if not files:
        return
    result = git("diff", "--cached", "--name-status", "--", *files)
    if result.returncode != 0:
        return
    allowed = _ACTION_TO_STATUS_CODES[action]
    mismatches = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        # First token is the status code (A/M/D or R<score>/C<score>).
        code = parts[0][:1]
        path = parts[-1] if len(parts) > 1 else line
        if code not in allowed:
            mismatches.append(
                f"{path}: staged as '{code}' but action is '{action}'"
            )
    if mismatches:
        raise RuntimeError(
            f"Step action '{action}' does not match the staged changes: "
            f"{'; '.join(mismatches)}. The subprocess likely performed a "
            f"different operation than declared (e.g. a 'modify' that "
            f"deleted the file, or a 'create' that modified an existing "
            f"one). Fix the plan's action or the subprocess prompt."
        )


def commit_files(files: list, message: str, action: str = ""):
    """Stage specific files and commit.

    Before committing, verify the add actually staged a change. If the
    subprocess claimed to edit a file but didn't (e.g. decided no edit was
    needed, wrote to the wrong path, or silently failed), `git commit` would
    fail with "no changes added to commit" — an error that historically
    looked identical to a real git failure and masked subprocess bugs. Fail
    fast with a distinct error in that case.

    If `action` is provided, also cross-check that the staged changes
    match the declared action (#237): a 'modify' action must not produce
    a deletion, a 'delete' action must not produce an addition, etc.
    """
    if not files:
        return
    git("add", *files)
    staged = git("diff", "--cached", "--quiet", "--", *files)
    if staged.returncode == 0:
        # `git diff --cached --quiet` exits 0 when there is no diff, meaning
        # nothing was actually staged for these files.
        raise RuntimeError(
            f"Subprocess made no changes to expected files ({', '.join(files)}). "
            f"The step's implementation subprocess either decided no edit was needed, "
            f"edited a different path, or silently failed. Check the subprocess transcript."
        )
    if action:
        _assert_action_matches_staged(action, files)
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


_NON_COMMITTING_ACTIONS = frozenset({"test", "verify"})


def persist_execution_log(log_path: Path, execution_log: dict):
    """Atomically write the execution log to disk (#243).

    Used after every state mutation inside the run loop so a crash
    between steps leaves the log consistent with whichever step last
    completed. Without this, the log was only written at the very end
    of main() — a crash mid-loop meant git had the new commits but
    the log still reflected the previous run, producing the exact
    drift that validate_resume_state() now catches at the start of
    the next run.

    The atomic-write pattern (write to .tmp, os.replace) ensures the
    log file is never partially written even if the process dies
    mid-flush.
    """
    tmp = log_path.with_suffix(log_path.suffix + ".tmp")
    tmp.write_text(json.dumps(execution_log, indent=2), encoding="utf-8")
    os.replace(tmp, log_path)


def validate_resume_state(
    completed_step_numbers: set,
    previous_entries: dict,
    plan_steps: list,
) -> list:
    """Cross-check git commits against the previous execution log (#242).

    Git is authoritative — it's the actual record of what landed on the
    branch — and the execution log is derivative metadata for steps
    that can't be recovered from git (verify/test status, timing,
    subprocess errors). When the two disagree on which modifying steps
    are done, resume behavior would be undefined, so we refuse to
    proceed and ask the operator to reconcile by hand.

    Only MODIFYING steps (action in {create, modify, delete}) are
    cross-checked, because test/verify steps never commit by design
    and would false-positive on every resume.

    Disagreement shapes:
    - Log claims a modifying step 'passed' but no matching commit on
      the branch. This happens after a squash/rebase rewrote history
      while the log was left in place. Resume would skip the
      commit-less step and ship a branch missing it.
    - Log claims a modifying step 'failed'/'skipped' but a matching
      commit DOES exist. This happens after a hand-fix commit between
      runs. Resume would carry the step forward as 'passed' with a
      stale error message — the execution log would lie about what
      shipped.

    Returns a list of error strings (empty = consistent). Caller
    aborts on non-empty.
    """
    action_by_num = {}
    for step in plan_steps:
        if not isinstance(step, dict):
            continue
        num = step.get("step_number")
        if isinstance(num, int):
            action_by_num[num] = step.get("action", "")

    errors = []
    for num, entry in previous_entries.items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        # #244: an entry with status='started' means the previous run
        # was interrupted mid-step — partial work may exist in the
        # worktree, in a Claude session state dir, or as an orphan
        # commit. Resume cannot safely choose between 'skip this step'
        # and 'run it again' without knowing what landed. Always flag.
        if status == "started":
            errors.append(
                f"Step {num}: execution log says 'started' — the previous "
                f"run was interrupted mid-step. Inspect the branch and "
                f"worktree, then either commit/discard partial work and "
                f"delete the stale log entry, or delete the whole log "
                f"to re-run the step from scratch."
            )
            continue
        action = action_by_num.get(num, "")
        if action in _NON_COMMITTING_ACTIONS:
            # verify/test steps never commit by design — skip.
            continue
        in_git = num in completed_step_numbers
        if status == "passed" and not in_git:
            errors.append(
                f"Step {num}: execution log says 'passed' but no matching "
                f"commit exists on the branch. History may have been "
                f"squashed/rewritten. Delete the stale execution log or "
                f"restore the commit to resume."
            )
        elif status in ("failed", "skipped") and in_git:
            errors.append(
                f"Step {num}: execution log says '{status}' but a commit "
                f"matching this step exists on the branch. A hand-fix "
                f"commit likely landed between runs. Delete the stale "
                f"execution log to let git state drive resume."
            )
    return errors


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


def parse_verify_output(stdout: str) -> dict:
    """Parse a verify step's Claude Code output into a {passed, explanation} dict.

    Handles every envelope shape the runner has actually seen (#252):
    - Bare JSON object: {"passed": true, "explanation": "..."}
    - Claude Code envelope: {"result": "<inner text>"} — inner is then parsed
    - Markdown-fenced JSON: ```json\\n{...}\\n``` or ```\\n{...}\\n```
    - JSON embedded in prose: "Here's the result: {...}"
    - Combination: envelope → inner prose → fenced JSON
    - Anything unparseable falls through to {"passed": False, "explanation": "Unparseable output"}

    Extracted from execute_step for unit testability — the parser used
    to be inlined, which meant none of its branches had coverage and a
    silent regression could break every verify step across every plan.
    """
    verify = {"passed": False, "explanation": "Unparseable output"}
    try:
        envelope = json.loads(stdout)
        if isinstance(envelope, dict) and "result" in envelope:
            text = envelope["result"].strip()
        elif isinstance(envelope, dict) and "passed" in envelope:
            return envelope
        else:
            text = stdout.strip()

        # Strip markdown code fences if present.
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines).strip()
        # Find first { and last } to extract JSON from prose.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
                if isinstance(parsed, dict) and "passed" in parsed:
                    verify = parsed
            except json.JSONDecodeError:
                pass
    except (json.JSONDecodeError, TypeError):
        # stdout is not JSON at all — fall through and try to find a
        # raw JSON blob in the prose.
        text = stdout.strip() if isinstance(stdout, str) else ""
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
                if isinstance(parsed, dict) and "passed" in parsed:
                    verify = parsed
            except json.JSONDecodeError:
                pass
    return verify


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

                # #252: delegate to the standalone parser so every
                # envelope/fence/prose shape is exercised by unit tests.
                verify = parse_verify_output(stdout)

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

    # #242: cross-validate git commits against the previous execution log
    # before resuming. Disagreement means history was rewritten or a
    # hand-fix commit landed between runs — either way resume behavior
    # would be undefined, so abort and ask the operator to reconcile.
    resume_errors = validate_resume_state(
        completed_step_numbers, previous_entries, sorted_steps,
    )
    if resume_errors:
        print(
            "Resume state inconsistent — refusing to proceed:\n  "
            + "\n  ".join(resume_errors),
            file=sys.stderr,
        )
        sys.exit(1)

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
            persist_execution_log(log_path, execution_log)  # #243
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
            persist_execution_log(log_path, execution_log)  # #243
            continue

        print(f"Executing step {step_num}: {step.get('description', '')}", file=sys.stderr)
        # #235: refuse to run if the worktree has pre-existing dirty
        # state in any of this step's target files — otherwise the
        # user's uncommitted work would be silently committed as if
        # the runner authored it.
        try:
            step_files = step.get("files", [])
            if step.get("action") not in ("test", "verify"):
                assert_files_clean(step_files)
        except RuntimeError as e:
            step_result = {
                "step_number": step_num,
                "status": "failed",
                "files_changed": step.get("files", []),
                "error": str(e),
            }
            execution_log["steps"].append(step_result)
            failed_steps.add(step_num)
            aborted = True
            for s in sorted_steps:
                if s["step_number"] > step_num:
                    execution_log["steps"].append({
                        "step_number": s["step_number"],
                        "status": "skipped",
                        "files_changed": s.get("files", []),
                        "error": "Runner aborted",
                    })
            persist_execution_log(log_path, execution_log)  # #243
            break
        resolved_model = step.get("model") or default_model
        # #244: write a 'started' stub entry BEFORE the subprocess call
        # and persist it. If the runner is interrupted mid-step, this
        # marker survives and validate_resume_state() surfaces it on
        # the next run so the operator can hand-reconcile instead of
        # silently re-running the step from scratch.
        started_stub = {
            "step_number": step_num,
            "status": "started",
            "files_changed": step.get("files", []),
            "error": None,
        }
        execution_log["steps"].append(started_stub)
        persist_execution_log(log_path, execution_log)
        # #236: snapshot worktree state before the step so we can detect
        # writes to paths the step didn't declare in step.files.
        pre_state = snapshot_worktree_state()
        step_result = execute_step(step, spec, resolved_model)
        # Check for unexpected writes on any non-test/verify step that
        # succeeded — test/verify steps shell out and shouldn't write
        # files at all, but they're also not subject to the step.files
        # contract, so we skip the check for them.
        if (step_result["status"] == "passed"
                and step.get("action") not in ("test", "verify")):
            try:
                assert_no_unexpected_writes(
                    pre_state, snapshot_worktree_state(),
                    step.get("files", []),
                )
            except RuntimeError as e:
                step_result["status"] = "failed"
                step_result["error"] = str(e)
        # #244: overwrite the 'started' stub with the real step result
        # now that execute_step has returned. We know the stub is the
        # last appended entry because no other append happens between
        # the started-stub write and here.
        execution_log["steps"][-1] = step_result
        # #243: persist the step result BEFORE the commit attempt, so a
        # crash during commit_files leaves the log honest about what
        # execute_step produced. Combined with validate_resume_state,
        # this closes the drift window between git state and log state.
        persist_execution_log(log_path, execution_log)

        if step_result["status"] == "passed":
            # Commit if there are files
            files = step.get("files", [])
            if files and step.get("action") not in ("test", "verify"):
                try:
                    commit_files(
                        files,
                        f"runner/{uuid} step {step_num}: {step.get('description', '')}",
                        action=step.get("action", ""),
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
                    persist_execution_log(log_path, execution_log)  # #243
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
            persist_execution_log(log_path, execution_log)  # #243
            break

    if aborted:
        execution_log["status"] = "aborted"

    # Final write — also picks up the top-level status flip above.
    # Per-step writes happen inside the loop (#243) for durability.
    persist_execution_log(log_path, execution_log)

    if aborted:
        print(f"Runner aborted. Log: {log_path}", file=sys.stderr)
        # Return to original branch
        git("checkout", original_branch)
        sys.exit(1)

    print(f"Branch: {branch}")
    print(f"Log: {log_path}")


if __name__ == "__main__":
    main()
