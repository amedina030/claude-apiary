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
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .config_loader import get as cfg
from .schema_versions import (
    EXECUTION_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    assert_schema_version,
)

# Eager-import stage_lib (and transitively claude_subprocess / cost_emit) at
# module top. These modules MUST be resolved while the working tree is still
# on the parent branch (typically master). If we let run_claude() do a
# deferred import, Python loads the source AFTER executor.main() has done
# `git checkout runner/<uuid>`, which silently picks up whatever older
# copies happen to live on the runner branch and shadows every fix we
# ship on master. Loading them now caches the master versions in
# sys.modules so all later calls use them regardless of working-tree state.
from .stage_lib import (
    check_uuid_safe,
    extract_json,
)
from .stage_lib import (
    run_claude as _spawn_claude,
)
from .target_repo import executions_dir

SCRIPT_DIR = Path(__file__).resolve().parent
EXECUTIONS_DIR = executions_dir()

MAX_STEP_RETRIES = cfg("executor", "max_retries_per_step", 2)
MAX_NO_CHANGE_RETRIES = cfg("executor", "max_no_change_retries", 2)


class NoChangesError(RuntimeError):
    """Raised by commit_files when git add staged no diff for the expected files.

    Distinct from other commit failures so the caller can decide to retry
    the step with an augmented prompt (T-2026-119).
    """


# -- Git helpers (#253: shared via runner/git_lib.py) --

from core.utils.atomic import write_json_atomic

from .git_lib import (
    branch_exists,
    create_branch,
    git,
    run_branch_from_env,
)
from .git_lib import (
    current_branch as get_current_branch,
)
from .git_lib import (
    format_git_error as _format_git_error,
)


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
    pre: set,
    post: set,
    expected_files: list,
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
            mismatches.append(f"{path}: staged as '{code}' but action is '{action}'")
    if mismatches:
        raise RuntimeError(
            f"Step action '{action}' does not match the staged changes: "
            f"{'; '.join(mismatches)}. The subprocess likely performed a "
            f"different operation than declared (e.g. a 'modify' that "
            f"deleted the file, or a 'create' that modified an existing "
            f"one). Fix the plan's action or the subprocess prompt."
        )


def commit_files(files: list, message: str, action: str = ""):
    """Stage specific files and commit them on the current branch.

    This is the runner's single commit path — every file mutation that the
    executor records in git flows through here.  The function is intentionally
    strict: it fails fast on no-ops and on action/status mismatches so that
    upstream subprocess bugs surface immediately rather than producing silent
    empty commits or misattributed diffs.

    Preconditions (caller must ensure):
        - The working tree is on the correct runner branch.
        - ``files`` are paths relative to the repo root that the step was
          expected to create, modify, or delete.
        - No other staged changes exist outside ``files`` (the function does
          not reset the index — stale staged state will be included in the
          commit).
        - Target files are not dirty from unrelated work; call
          ``assert_files_clean()`` before the step to guarantee this (#235).

    Postconditions (on successful return):
        - Exactly one new commit exists on HEAD whose diff touches only
          ``files``.
        - The index is clean with respect to ``files``.

    Raises:
        RuntimeError: if ``git add`` stages no diff for ``files`` (subprocess
            made no changes), if the staged status codes conflict with
            ``action`` (see ``_assert_action_matches_staged``), or if
            ``git commit`` itself fails.
    """
    if not files:
        return
    git("add", *files)
    staged = git("diff", "--cached", "--quiet", "--", *files)
    if staged.returncode == 0:
        # `git diff --cached --quiet` exits 0 when there is no diff, meaning
        # nothing was actually staged for these files.
        raise NoChangesError(
            f"Subprocess made no changes to expected files ({', '.join(files)}). "
            f"The step's implementation subprocess either decided no edit was needed, "
            f"edited a different path, or silently failed. Check the subprocess transcript."
        )
    if action:
        _assert_action_matches_staged(action, files)
    result = git("commit", "-m", message)
    if result.returncode != 0:
        raise RuntimeError(
            _format_git_error(
                "committing",
                result,
                extra=f"staged files: {', '.join(files)}",
            )
        )


def _ensure_on_branch(branch: str) -> tuple[str, bool]:
    """Put the working tree on *branch*. Returns (previous branch, switched).

    Three cases, in order of how the runner actually reaches them:
      * already on it (detached mode — the orchestrator created the worktree
        on this branch): do nothing, so there is no second branch and no
        branch to restore on abort;
      * it exists (a resumed run): check it out;
      * it does not exist (interactive first run): create it.

    Exits the process on a git failure, since nothing downstream is safe once
    the working tree is on the wrong branch.
    """
    original_branch = get_current_branch()
    if original_branch == branch:
        return original_branch, False
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
    return original_branch, True


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
    mid-flush. ``core.utils.atomic`` owns that pattern.
    """
    write_json_atomic(log_path, execution_log, indent=2)


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


def verify_post_conditions(step: dict, repo_root: Path) -> list:
    """Return a list of human-readable failure strings for unmet post_conditions.

    Empty list = all conditions satisfied (or the step has none declared).
    Checks operate on the live filesystem, not git state, so a condition
    satisfied by a prior step's commit counts as met — that's the whole
    point (#T-2026-122 phase 2): success is measured by end-state, not by
    which step's subprocess happened to make the change.
    """
    conds = step.get("post_conditions")
    if not conds or not isinstance(conds, list):
        return []
    failures = []
    for j, cond in enumerate(conds):
        if not isinstance(cond, dict):
            continue
        ctype = cond.get("type")
        fpath = cond.get("file", "")
        if not isinstance(fpath, str) or not fpath:
            continue
        target = repo_root / fpath
        if ctype == "file_exists":
            if not target.exists():
                failures.append(f"[{j}] file_exists: '{fpath}' does not exist")
        elif ctype == "file_absent":
            if target.exists():
                failures.append(f"[{j}] file_absent: '{fpath}' still exists")
        elif ctype in ("file_contains", "file_lacks"):
            text = cond.get("text", "")
            if not target.exists():
                failures.append(f"[{j}] {ctype}: '{fpath}' does not exist (cannot check text)")
                continue
            try:
                body = target.read_text(encoding="utf-8")
            except OSError as e:
                failures.append(f"[{j}] {ctype}: cannot read '{fpath}': {e}")
                continue
            present = text in body
            if ctype == "file_contains" and not present:
                failures.append(f"[{j}] file_contains: '{fpath}' missing expected text {text!r}")
            elif ctype == "file_lacks" and present:
                failures.append(
                    f"[{j}] file_lacks: '{fpath}' still contains forbidden text {text!r}"
                )
    return failures


def files_touched_by_prior_steps(uuid: str, files: list) -> dict:
    """Map each file in ``files`` to the list of prior runner step numbers
    that committed changes to it on the current branch.

    Used to distinguish "subsumed" no-change steps (prior step already did
    the work) from genuine executor failures. Commit subjects follow the
    pattern ``runner/<uuid> step <N>: ...`` so we filter on that prefix
    and pass ``-- <file>`` to git log so only commits touching the file
    appear.
    """
    if not files:
        return {}
    prefix = f"runner/{uuid} step "
    touched = {}
    for file in files:
        touched[file] = []
        result = git("log", "--format=%s", "HEAD", "--", file)
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            if not line.startswith(prefix):
                continue
            rest = line[len(prefix) :]
            num_str = rest.split(":", 1)[0].strip()
            try:
                touched[file].append(int(num_str))
            except ValueError:
                continue
    return touched


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
        rest = line[len(prefix) :]
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


def build_step_prompt(step: dict, spec: dict, retry_hint: str = "") -> str:
    """Build the prompt for a create/modify/delete step.

    ``retry_hint``, when non-empty, is appended after the standard
    instructions. The executor uses this to nudge a retry attempt after
    a previous attempt produced no file changes (T-2026-119).
    """
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
        parts.append(
            "Create the file(s) listed above with the implementation described in the code specification."
        )
    elif action == "modify":
        parts.append(
            "Read the existing file(s) listed above and apply the changes described in the code specification."
        )
    elif action == "delete":
        parts.append("Delete the file(s) listed above as described in the code specification.")

    parts.extend(
        [
            "",
            "Write the actual code — not pseudocode, not explanations. Just implement it.",
            "Use the existing codebase patterns and conventions.",
        ]
    )

    if retry_hint:
        parts.extend(["", retry_hint])

    return "\n".join(parts)


def build_verify_prompt(step: dict, spec: dict) -> str:
    """Build the prompt for a verify step."""
    return "\n".join(
        [
            f"You are verifying step {step['step_number']} of a plan.",
            "",
            f"## Verification: {step['description']}",
            "",
            "## What to check",
            step.get("code_spec", ""),
            "",
            "## Instructions",
            "",
            "Read the relevant files and confirm whether the acceptance criterion is met.",
            'Output ONLY a JSON object: {"passed": true/false, "explanation": "brief reason"}',
        ]
    )


def parse_verify_output(stdout: str) -> dict:
    """Parse a verify step's Claude Code output into a {passed, explanation} dict.

    Every envelope shape the runner has actually seen (#252) — bare JSON, the
    Claude Code ``{"result": ...}`` envelope, markdown fences (closed or not),
    JSON embedded in prose, and combinations — is handled by the one shared
    salvager in ``stage_lib``. Anything that does not yield an object with a
    ``passed`` key falls through to a failed verdict rather than guessing.
    """
    try:
        parsed = extract_json(stdout, require_keys=("passed",), allow_list=False)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and "passed" in parsed:
        return parsed
    return {"passed": False, "explanation": "Unparseable output"}


def run_claude(prompt: str, model: str) -> tuple[int, str, str]:
    """Run Claude Code subprocess with the specified model."""
    return _spawn_claude(prompt, timeout=cfg("executor", "timeout", 900), model=model)


def run_test_command(code_spec: str) -> tuple[bool, str]:
    """Execute a test command from code_spec. Returns (passed, output)."""
    command = code_spec.strip()
    if not command:
        return False, "No test command in code_spec"
    # Handle 'cd <dir> && <real_command>' — planners emit this for worktree
    # paths, but we can't use shell=True. Extract cwd and run the rest.
    cwd = None
    cd_match = re.match(r"^cd\s+(\S+)\s*&&\s*(.+)$", command)
    if cd_match:
        cwd = cd_match.group(1)
        command = cd_match.group(2).strip()
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return False, f"could not parse test command: {e}"
    if not argv:
        return False, "No test command in code_spec"
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=cwd,
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as e:
        return (
            False,
            f"test command not found: {argv[0]} — code_spec must be a single command starting with an executable on PATH ({e})",
        )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def execute_step(step: dict, spec: dict, model: str, retry_hint: str = "") -> dict:
    """Execute a single step. Returns a step result dict.

    ``retry_hint`` is forwarded to build_step_prompt for create/modify/delete
    actions, used by the runner to nudge a no-changes retry (T-2026-119).
    """
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
                result["transcript"] = {"stdout": stdout, "stderr": stderr, "rc": rc}
                if rc != 0:
                    result["error"] = (
                        f"Claude Code error (attempt {attempt}): {stderr.strip()[:500]}"
                    )
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
                    result["error"] = (
                        f"Verify failed (attempt {attempt}): {verify.get('explanation', 'unknown')}"
                    )

            else:
                # create/modify/delete
                prompt = build_step_prompt(step, spec, retry_hint=retry_hint)
                rc, stdout, stderr = run_claude(prompt, model)
                result["transcript"] = {"stdout": stdout, "stderr": stderr, "rc": rc}
                if rc != 0:
                    result["error"] = (
                        f"Claude Code error (attempt {attempt}): {stderr.strip()[:500]}"
                    )
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


def load_plan(plan_path: Path) -> tuple:
    """Read and gate the plan artifact. Returns ``(plan, uuid)``.

    Exits 1 on a missing file, invalid JSON, a schema-version mismatch, a plan
    the validator rejected, or a uuid that is not a safe filename component.
    """
    if not plan_path.exists():
        print(f"Plan file not found: {plan_path}", file=sys.stderr)
        sys.exit(1)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid plan JSON: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        assert_schema_version(plan, "plan", PLAN_SCHEMA_VERSION)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not plan.get("valid", False):
        print("Plan is not valid -- cannot execute invalid plan", file=sys.stderr)
        sys.exit(1)
    # ATK-005: validate uuid from plan JSON to prevent path traversal
    try:
        uuid = check_uuid_safe(plan.get("uuid", plan_path.stem), "Plan uuid")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    return plan, uuid


def prepare_resume(uuid: str, sorted_steps: list, log_path: Path) -> tuple:
    """Reconcile git history with the previous execution log before resuming.

    Returns ``(completed_step_numbers, previous_entries)``. Git is
    authoritative — it is the record of what landed — and the log is
    derivative metadata for steps that never commit. Exits 1 when the two
    disagree (#242): resume behaviour would be undefined, so the operator
    reconciles by hand.
    """
    completed_step_numbers = get_completed_step_numbers(uuid)
    if completed_step_numbers:
        print(
            f"Resume: skipping {len(completed_step_numbers)} already-committed "
            f"step(s): {sorted(completed_step_numbers)}",
            file=sys.stderr,
        )
    # Read the prior log BEFORE we start writing the new one: it is the only
    # source for verify/test step status, which git log cannot recover.
    previous_entries = load_previous_log(log_path)
    resume_errors = validate_resume_state(
        completed_step_numbers,
        previous_entries,
        sorted_steps,
    )
    if resume_errors:
        print(
            "Resume state inconsistent — refusing to proceed:\n  " + "\n  ".join(resume_errors),
            file=sys.stderr,
        )
        sys.exit(1)
    return completed_step_numbers, previous_entries


def _carried_forward_entry(step: dict, previous_entries: dict) -> dict:
    """The log entry for a step whose commit is already on the branch.

    Prefers the previous run's richer entry (real files_changed, timing) over
    a bland stub; re-running the step would produce "nothing to commit" and
    abort the run.
    """
    note = "carried forward from previous run (commit on branch)"
    prior = previous_entries.get(step["step_number"])
    if isinstance(prior, dict) and prior.get("status") == "passed":
        return dict(prior, note=note)
    return {
        "step_number": step["step_number"],
        "status": "passed",
        "files_changed": step.get("files", []),
        "error": None,
        "note": note,
    }


def _mark_remaining_skipped(
    execution_log: dict, sorted_steps: list, after: int | None = None
) -> None:
    """Append 'Runner aborted' entries for the steps that will never run.

    ``after`` marks everything with a higher step_number; without it, every
    step that has no entry yet.
    """
    if after is None:
        logged = {s["step_number"] for s in execution_log["steps"]}
        remaining = [s for s in sorted_steps if s["step_number"] not in logged]
    else:
        remaining = [s for s in sorted_steps if s["step_number"] > after]
    for step in remaining:
        execution_log["steps"].append(
            {
                "step_number": step["step_number"],
                "status": "skipped",
                "files_changed": step.get("files", []),
                "error": "Runner aborted",
            }
        )


def run_step_with_commit(
    step: dict, spec: dict, model: str, uuid: str, execution_log: dict, log_path: Path
) -> tuple:
    """Execute one step, verify it, and commit it. Returns ``(result, error)``.

    ``error`` is non-None when the commit itself failed and the run must
    abort. The loop exists for T-2026-119: when commit_files finds no diff
    (the subprocess succeeded but made no edits) the step is retried with an
    augmented prompt up to MAX_NO_CHANGE_RETRIES times, because the
    subprocess is the only thing that can actually write the files.

    The log entry is rewritten and persisted after every state change (#243)
    so a crash leaves the log honest about what the step produced.
    """
    step_num = step["step_number"]
    retry_hint = ""
    no_change_attempts = 0

    while True:
        # #236: snapshot worktree state before the step so we can detect
        # writes to paths the step didn't declare in step.files.
        pre_state = snapshot_worktree_state()
        step_result = execute_step(step, spec, model, retry_hint=retry_hint)
        # Unexpected-write check on any non-test/verify step that succeeded —
        # test/verify steps shell out and shouldn't write files at all, and
        # they aren't subject to the step.files contract.
        if step_result["status"] == "passed" and step.get("action") not in ("test", "verify"):
            try:
                assert_no_unexpected_writes(
                    pre_state,
                    snapshot_worktree_state(),
                    step.get("files", []),
                )
            except RuntimeError as e:
                step_result["status"] = "failed"
                step_result["error"] = str(e)
        # #244: overwrite the 'started' stub with the real result. It is the
        # last appended entry — nothing else appends in between.
        execution_log["steps"][-1] = step_result
        persist_execution_log(log_path, execution_log)

        if step_result["status"] != "passed":
            return step_result, None

        # Post-conditions (#T-2026-122 phase 2) run BEFORE the commit, so a
        # step whose declared post-state is wrong is never committed. They
        # read the live filesystem, so a condition a prior step already
        # satisfied counts as met.
        pc_failures = verify_post_conditions(step, Path.cwd())
        if pc_failures:
            step_result["status"] = "failed"
            step_result["error"] = "Post-condition(s) not satisfied after step: " + "; ".join(
                pc_failures
            )
            execution_log["steps"][-1] = step_result
            persist_execution_log(log_path, execution_log)
            return step_result, None

        files = step.get("files", [])
        if not files or step.get("action") in ("test", "verify"):
            return step_result, None

        try:
            commit_files(
                files,
                f"runner/{uuid} step {step_num}: {step.get('description', '')}",
                action=step.get("action", ""),
            )
            return step_result, None
        except NoChangesError as e:
            # Phase-2 path: declared post_conditions are satisfied (they were
            # checked above), so the end state is correct whoever produced it
            # — accept as a no-op success without a new commit.
            if step.get("post_conditions"):
                print(
                    f"Step {step_num}: no new changes but declared "
                    f"post-conditions already satisfied — accepting as a "
                    f"no-op success.",
                    file=sys.stderr,
                )
                step_result["subsumed_by"] = "post_conditions"
                execution_log["steps"][-1] = step_result
                persist_execution_log(log_path, execution_log)
                return step_result, None

            # Phase-1 fallback (no post_conditions): if prior steps on this
            # branch already committed every file this step declares, the
            # subprocess correctly did nothing — the planner over-decomposed
            # (T-2026-122). status stays 'passed'; subsumed_by is the audit
            # trail.
            touched = files_touched_by_prior_steps(uuid, files)
            if touched and all(touched.get(f) for f in files):
                subsuming = sorted({n for nums in touched.values() for n in nums})
                print(
                    f"Step {step_num}: no new changes — target files "
                    f"({', '.join(files)}) already modified by prior "
                    f"step(s) {subsuming}. Marking subsumed.",
                    file=sys.stderr,
                )
                step_result["subsumed_by"] = subsuming
                execution_log["steps"][-1] = step_result
                persist_execution_log(log_path, execution_log)
                return step_result, None

            no_change_attempts += 1
            if no_change_attempts > MAX_NO_CHANGE_RETRIES:
                return step_result, str(e)
            print(
                f"Step {step_num}: subprocess produced no changes — "
                f"retrying ({no_change_attempts}/{MAX_NO_CHANGE_RETRIES}) "
                f"with Edit-tool nudge",
                file=sys.stderr,
            )
            retry_hint = (
                "IMPORTANT: A previous attempt made no file changes. "
                "You MUST use the Edit or Write tool to actually modify "
                f"the files listed above ({', '.join(files)}). Do not "
                "just describe or plan the change — execute the tool call."
            )
        except RuntimeError as e:
            return step_result, str(e)


def execute_plan(
    *,
    sorted_steps,
    spec,
    default_model,
    uuid,
    branch,
    completed_step_numbers,
    previous_entries,
    log_path,
) -> dict:
    """Run every step in dependency order and return the execution log."""
    execution_log = {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "uuid": uuid,
        "branch": branch,
        "status": "completed",
        "steps": [],
    }
    failed_steps = set()

    for step in sorted_steps:
        step_num = step["step_number"]

        if step_num in completed_step_numbers:
            execution_log["steps"].append(_carried_forward_entry(step, previous_entries))
            persist_execution_log(log_path, execution_log)  # #243
            continue

        if any(d in failed_steps for d in step.get("depends_on", [])):
            execution_log["steps"].append(
                {
                    "step_number": step_num,
                    "status": "skipped",
                    "files_changed": step.get("files", []),
                    "error": "Dependency failed or was skipped",
                }
            )
            failed_steps.add(step_num)
            persist_execution_log(log_path, execution_log)  # #243
            continue

        print(f"Executing step {step_num}: {step.get('description', '')}", file=sys.stderr)
        # #235: refuse to run if the worktree has pre-existing dirty state in
        # any of this step's target files — otherwise the operator's
        # uncommitted work is committed as if the runner authored it.
        try:
            if step.get("action") not in ("test", "verify"):
                assert_files_clean(step.get("files", []))
        except RuntimeError as e:
            execution_log["steps"].append(
                {
                    "step_number": step_num,
                    "status": "failed",
                    "files_changed": step.get("files", []),
                    "error": str(e),
                }
            )
            failed_steps.add(step_num)
            _mark_remaining_skipped(execution_log, sorted_steps, after=step_num)
            execution_log["status"] = "aborted"
            persist_execution_log(log_path, execution_log)  # #243
            return execution_log

        # #244: a 'started' stub written and persisted BEFORE the subprocess
        # call. If the runner is interrupted mid-step it survives, and
        # validate_resume_state() surfaces it next run so the operator
        # reconciles instead of silently re-running the step.
        execution_log["steps"].append(
            {
                "step_number": step_num,
                "status": "started",
                "files_changed": step.get("files", []),
                "error": None,
            }
        )
        persist_execution_log(log_path, execution_log)

        step_result, commit_error = run_step_with_commit(
            step,
            spec,
            step.get("model") or default_model,
            uuid,
            execution_log,
            log_path,
        )

        if commit_error is not None:
            print(f"Git error: {commit_error}", file=sys.stderr)
            step_result["status"] = "failed"
            step_result["error"] = commit_error
            execution_log["steps"][-1] = step_result
            failed_steps.add(step_num)
            _mark_remaining_skipped(execution_log, sorted_steps, after=step_num)
            execution_log["status"] = "aborted"
            persist_execution_log(log_path, execution_log)  # #243
            return execution_log

        if step_result["status"] != "passed":
            failed_steps.add(step_num)
            _mark_remaining_skipped(execution_log, sorted_steps)
            execution_log["status"] = "aborted"
            persist_execution_log(log_path, execution_log)  # #243
            return execution_log

    return execution_log


def main():
    parser = argparse.ArgumentParser(description="Executor — runner stage 4")
    parser.add_argument("plan", help="Path to plan JSON file")
    args = parser.parse_args()

    plan, uuid = load_plan(Path(args.plan))

    # One branch per run. The orchestrator names it (APIARY_RUNNER_BRANCH) and,
    # in detached mode, has already created the worktree on it; this stage used
    # to `checkout -b runner/<uuid>` regardless, producing a second branch that
    # nothing else in the pipeline knew about — the worktree branch was left
    # pointing at master, the morning queue table joined on the wrong name, and
    # each failed run consumed two max_unreviewed slots (review runner Bug 3).
    # A standalone `python -m runner.executor <plan>` falls back to
    # runner/<uuid>, which is what it always was.
    branch = run_branch_from_env(uuid)
    original_branch, switched = _ensure_on_branch(branch)

    sorted_steps = topo_sort(plan.get("steps", []))

    EXECUTIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = EXECUTIONS_DIR / f"{uuid}.json"
    completed_step_numbers, previous_entries = prepare_resume(uuid, sorted_steps, log_path)

    execution_log = execute_plan(
        sorted_steps=sorted_steps,
        spec=plan.get("spec", {}),
        default_model=plan.get("executor_model", "sonnet"),
        uuid=uuid,
        branch=branch,
        completed_step_numbers=completed_step_numbers,
        previous_entries=previous_entries,
        log_path=log_path,
    )

    # Final write — per-step writes happen inside the loop (#243) for
    # durability; this one also picks up the top-level status.
    persist_execution_log(log_path, execution_log)

    if execution_log["status"] == "aborted":
        print(f"Runner aborted. Log: {log_path}", file=sys.stderr)
        # Return to the branch we were on — but only if we actually left it.
        # In detached mode the worktree was already on the run branch, and
        # checking "back" to it here is what stranded the failed run's commits
        # on a branch nobody named.
        if switched:
            git("checkout", original_branch)
        sys.exit(1)

    print(f"Branch: {branch}")
    print(f"Log: {log_path}")


if __name__ == "__main__":
    main()
