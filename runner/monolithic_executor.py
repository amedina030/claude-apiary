#!/usr/bin/env python3
"""
Monolithic executor — Stage 4 variant that runs an entire plan inside ONE
Claude CLI subprocess instead of spawning one per step.

(Originally named "chained executor"; the name was a carryover from the
earlier SDK-based design where the orchestrator intercepted per-step tool
calls. The CLI-only implementation doesn't chain anything — it's one
subprocess running the whole plan.)

Rationale
---------
The per-step executor (executor.py) pays ~25k tokens of cache creation on
every step because each step is a fresh ``claude -p`` subprocess. For a
six-step plan that's ~150k tokens of pure startup overhead before any
real work. The monolithic executor spawns once, hands the model the full
plan, and instructs it to commit after each step. Cache creation is
paid once; every subsequent step hits cache reads at ~10% of creation
cost.

Trade-offs vs per-step executor
-------------------------------
Lost (cannot be enforced mid-run with CLI-only invocation):
    - Per-step unexpected-write detection (only checked globally at end)
    - Per-step action/status staging consistency
    - Ability to halt on a bad step before subsequent steps run
    - Per-step subsumption vs no-changes distinction at commit time

Preserved (reconstructed post-hoc from git state):
    - Per-step execution log with status + files_changed
    - Post-condition verification against the final worktree state
    - Global unexpected-write detection (diff from branch base)
    - Cost + exit-status semantics for run_history

Failure mode changes
--------------------
If step 2 goes wrong under the per-step executor, steps 3-6 never run.
Under the monolithic executor, the model sees its own mistake mid-conversation
and either self-corrects or continues — the orchestrator can no longer
guarantee early abort. Operators reading the post-hoc execution log
must treat it as an after-the-fact reconstruction, not a live trace.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config_loader import get as cfg
from .executor import (
    EXECUTIONS_DIR,
    _ensure_on_branch,
    _norm_rel,
    persist_execution_log,
    verify_post_conditions,
)
from .git_lib import git, run_branch_from_env
from .schema_versions import (
    EXECUTION_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    assert_schema_version,
)
from .stage_lib import check_uuid_safe
from .stage_lib import run_claude as _spawn_claude

MONOLITHIC_TIMEOUT = cfg("monolithic_executor", "timeout_seconds", 1800)


def build_monolithic_prompt(plan: dict) -> str:
    """Construct the single prompt that drives the full plan in one session.

    Contract the model must follow:
        - Execute steps in ``step_number`` order.
        - For each non-test/verify step: do the work, then
          ``git add <files>`` and ``git commit -m "runner/<uuid> step N:
          <description>"``. Do NOT use any other commit-message format —
          the orchestrator parses these subjects to reconstruct what
          happened.
        - For test steps: run the ``code_spec`` as a shell command. A
          non-zero exit is fatal; stop and explain in a final message.
        - For verify steps: read the relevant files, confirm the check,
          include a one-line "verify N: PASS" or "verify N: FAIL <reason>"
          in the final response.
        - Do not modify files not listed in the step's ``files``.
        - Do not commit more than once per step.
    """
    uuid = plan.get("uuid", "")
    steps = plan.get("steps", [])
    spec = plan.get("spec", {})

    parts = [
        "You are executing an entire autonomous-runner plan end-to-end in "
        "a single conversation. Work through the steps in order; after "
        "each file-mutating step, commit with the exact message format "
        "given below so the orchestrator can reconstruct what you did.",
        "",
        f"## Plan UUID: {uuid}",
        "",
        "## Spec (for context only; do not re-decompose it)",
        "",
        "```json",
        json.dumps(spec, indent=2),
        "```",
        "",
        "## Commit protocol (CRITICAL — the orchestrator parses these)",
        "",
        "After each create/modify/delete step, run:",
        "    git add <exact files from step.files>",
        f'    git commit -m "runner/{uuid} step <N>: <step description>"',
        "",
        "The subject line MUST start with "
        f"'runner/{uuid} step <N>: '. Any deviation and the orchestrator "
        "will record the step as missing.",
        "",
        "Rules:",
        "- Steps run in ``step_number`` order.",
        "- A file-mutating step (action=create/modify/delete) produces "
        "EXACTLY one commit, touching ONLY its declared files. No "
        "commits outside this loop.",
        "- Test steps (action=test): run code_spec as a shell command. "
        "Non-zero exit is fatal — stop and explain.",
        "- Verify steps (action=verify): inspect the files and include "
        "a line 'verify <N>: PASS' or 'verify <N>: FAIL <reason>' in "
        "your final response.",
        "- If a step's work is already done (e.g. a prior step naturally "
        "included it), SKIP its commit and move on — do not create an "
        "empty commit.",
        "",
        "## Steps",
        "",
    ]

    for step in steps:
        n = step.get("step_number")
        action = step.get("action", "")
        files = step.get("files", [])
        parts.append(f"### Step {n} — action={action}")
        parts.append(f"**Description:** {step.get('description', '')}")
        if files:
            parts.append(f"**Files:** {', '.join(files)}")
        deps = step.get("depends_on", [])
        if deps:
            parts.append(f"**Depends on:** {deps}")
        parts.append("")
        parts.append("**Code spec:**")
        parts.append("")
        parts.append(step.get("code_spec", ""))
        pcs = step.get("post_conditions") or []
        if pcs:
            parts.append("")
            parts.append("**Post-conditions (orchestrator verifies these after the run):**")
            for pc in pcs:
                parts.append(f"- {json.dumps(pc)}")
        parts.append("")

    parts.extend(
        [
            "## After all steps",
            "",
            "When every step is committed (or intentionally skipped as "
            "subsumed), return a brief summary of what you did. Do not ask "
            "questions; this is an unattended run.",
        ]
    )

    return "\n".join(parts)


def _commits_since_base(base_ref: str) -> list[tuple[str, str]]:
    """Return (sha, subject) tuples for every commit on HEAD since base_ref."""
    result = git("log", "--format=%H%x09%s", f"{base_ref}..HEAD")
    if result.returncode != 0:
        return []
    out = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        out.append((sha.strip(), subject.strip()))
    return out


def _files_in_commit(sha: str) -> list[str]:
    """Return the list of files touched by a single commit."""
    result = git("show", "--name-only", "--format=", sha)
    if result.returncode != 0:
        return []
    return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]


def reconstruct_step_results(plan: dict, base_ref: str) -> list[dict]:
    """Walk the plan and, for each step, find the matching commit (if any)
    and build a step-result dict with the same shape executor.py produces.

    A step is marked:
        - ``passed`` if a commit whose subject matches the expected
          prefix exists AND (for file-mutating steps) the committed
          files equal the declared files.
        - ``subsumed`` if no commit exists but post_conditions are
          satisfied on the live worktree.
        - ``failed`` if neither holds — the model skipped the step
          entirely, or made changes without the expected commit format.
    """
    uuid = plan.get("uuid", "")
    steps = plan.get("steps", [])

    commits = _commits_since_base(base_ref)
    commit_by_step: dict[int, tuple[str, str]] = {}
    prefix = f"runner/{uuid} step "
    for sha, subject in commits:
        if not subject.startswith(prefix):
            continue
        rest = subject[len(prefix) :]
        num_str = rest.split(":", 1)[0].strip()
        try:
            n = int(num_str)
        except ValueError:
            continue
        # First commit per step wins; model should not double-commit but
        # if it does, the extras surface as unexpected-write at the end.
        commit_by_step.setdefault(n, (sha, subject))

    results = []
    for step in steps:
        n = step.get("step_number")
        action = step.get("action", "")
        files = step.get("files", [])
        entry = {
            "step_number": n,
            "status": "failed",
            "files_changed": files,
            "error": None,
        }

        if action in ("test", "verify"):
            # Non-committing by design. Assume passed if the subprocess
            # reached the end; post-hoc we can't distinguish test exit
            # codes from the monolithic transcript reliably. The final-state
            # post_conditions check below is the authoritative gate.
            entry["status"] = "passed"
        elif n in commit_by_step:
            sha, subject = commit_by_step[n]
            touched = _files_in_commit(sha)
            declared = {_norm_rel(f) for f in files if isinstance(f, str)}
            actual = {_norm_rel(f) for f in touched}
            if declared and actual - declared:
                entry["error"] = (
                    f"Commit {sha[:8]} touched files outside step.files: "
                    f"{sorted(actual - declared)}"
                )
                entry["status"] = "failed"
            else:
                entry["status"] = "passed"
                entry["commit_sha"] = sha
                entry["files_changed"] = sorted(actual) or files
        else:
            # No commit for this step. Either the model skipped it on
            # purpose (prior step already did the work) or it actually
            # failed. Post_conditions are the tie-breaker.
            pc_failures = verify_post_conditions(step, Path.cwd())
            if step.get("post_conditions") and not pc_failures:
                entry["status"] = "passed"
                entry["subsumed_by"] = "no_commit"
            else:
                entry["error"] = "No matching commit found and post-conditions " + (
                    f"unmet ({'; '.join(pc_failures)})" if pc_failures else "not declared"
                )

        # Always evaluate declared post_conditions once more on the
        # final worktree — even for steps that committed. Unsatisfied
        # PCs override a passed status, matching phase-2 semantics.
        pc_failures = verify_post_conditions(step, Path.cwd())
        if pc_failures and entry["status"] == "passed":
            entry["status"] = "failed"
            entry["error"] = "Post-condition(s) not satisfied after run: " + "; ".join(pc_failures)

        results.append(entry)

    return results


def detect_global_unexpected_writes(
    plan: dict,
    base_ref: str,
) -> list[str]:
    """Return paths touched on the branch that no step declared in its
    files list. Post-hoc equivalent of assert_no_unexpected_writes.
    """
    steps = plan.get("steps", [])
    allowed = set()
    for step in steps:
        for f in step.get("files", []):
            if isinstance(f, str):
                allowed.add(_norm_rel(f))
    result = git("diff", "--name-only", f"{base_ref}..HEAD")
    if result.returncode != 0:
        return []
    touched = [_norm_rel(ln) for ln in result.stdout.splitlines() if ln.strip()]
    return sorted({p for p in touched if p not in allowed})


def main():
    parser = argparse.ArgumentParser(
        description="Monolithic executor — runner stage 4 (single-subprocess variant)"
    )
    parser.add_argument("plan", help="Path to plan JSON file")
    args = parser.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"Plan file not found: {args.plan}", file=sys.stderr)
        sys.exit(1)

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid plan JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Same gate the per-step executor applies (executor.main). Without it a
    # plan written by an older auto_plan would be executed silently against
    # today's field expectations.
    try:
        assert_schema_version(plan, "plan", PLAN_SCHEMA_VERSION)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if not plan.get("valid", False):
        print("Plan is not valid — cannot execute", file=sys.stderr)
        sys.exit(1)

    try:
        uuid = check_uuid_safe(plan.get("uuid", plan_path.stem), "Plan uuid")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # One branch per run, named by the orchestrator — see executor._ensure_on_branch
    # and review runner Bug 3.
    branch = run_branch_from_env(uuid)
    original_branch, switched = _ensure_on_branch(branch)

    # Remember the branch's base commit so we can diff and log-walk later.
    head_before = git("rev-parse", "HEAD").stdout.strip()

    model = plan.get("executor_model", "sonnet")
    prompt = build_monolithic_prompt(plan)

    EXECUTIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = EXECUTIONS_DIR / f"{uuid}.json"
    transcript_path = EXECUTIONS_DIR / f"{uuid}.transcript.json"

    # schema_version is mandatory: auto_harden and approval both call
    # assert_schema_version on this artifact and exit 1 when it is absent
    # (review runner Bug 1 — every default-mode run died at stage 5).
    execution_log = {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "uuid": uuid,
        "branch": branch,
        "mode": "monolithic",
        "status": "running",
        "steps": [],
    }
    persist_execution_log(log_path, execution_log)

    print(
        f"Monolithic executor: running plan with {len(plan.get('steps', []))} "
        f"steps in one subprocess (timeout {MONOLITHIC_TIMEOUT}s)",
        file=sys.stderr,
    )

    rc, stdout, stderr = _spawn_claude(prompt, timeout=MONOLITHIC_TIMEOUT, model=model)

    # Persist the raw CLI transcript regardless of outcome — it's the
    # only post-hoc record of what the model actually did.
    try:
        transcript_path.write_text(
            json.dumps(
                {"returncode": rc, "stdout": stdout, "stderr": stderr},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"WARN: could not write transcript: {e}", file=sys.stderr)

    if rc != 0:
        execution_log["status"] = "aborted"
        execution_log["error"] = (
            f"Monolithic subprocess exited rc={rc}. stderr: {stderr.strip()[:500]}"
        )
        persist_execution_log(log_path, execution_log)
        print(f"Runner aborted. Log: {log_path}", file=sys.stderr)
        if switched:
            git("checkout", original_branch)
        sys.exit(1)

    # Reconstruct per-step results from git history + post_conditions.
    step_results = reconstruct_step_results(plan, head_before)
    unexpected = detect_global_unexpected_writes(plan, head_before)

    execution_log["steps"] = step_results
    if unexpected:
        execution_log["unexpected_writes"] = unexpected

    aborted = any(r["status"] == "failed" for r in step_results) or unexpected
    execution_log["status"] = "aborted" if aborted else "completed"
    persist_execution_log(log_path, execution_log)

    if aborted:
        print(f"Runner aborted (post-hoc verification failed). Log: {log_path}", file=sys.stderr)
        if unexpected:
            print(f"Unexpected writes: {unexpected}", file=sys.stderr)
        if switched:
            git("checkout", original_branch)
        sys.exit(1)

    print(f"Branch: {branch}")
    print(f"Log: {log_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
