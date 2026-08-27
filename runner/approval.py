#!/usr/bin/env python3
"""
Approval — Stage 6 of the runner.

Reads the harden verdict and either:
  - all_resolved: squash merge to master (locally — never pushes), write decision note
  - has_unresolved: leave branch, write TODO note

Produces a final runner report aggregating all stage artifacts.
No LLM calls — purely mechanical.

Output: runner/reports/<uuid>.json + scribe note

Usage:
    approval.py <path_to_harden_result.json>
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .target_repo import (
    executions_dir,
    intake_dir,
    plans_dir,
    reports_dir,
    specs_dir,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
REPORTS_DIR = reports_dir()
NOTES_SCRIPT = REPO_DIR / "scribe" / "notes.py"


# -- Git helpers (#253: shared via runner/git_lib.py) --

from .git_lib import checkout, git
from .schema_versions import (
    EXECUTION_SCHEMA_VERSION,
    HARDEN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    SPEC_SCHEMA_VERSION,
    assert_schema_version,
)
from .stage_lib import (
    extract_json_str as extract_json_from_text,
    extract_text,
    iter_unique,
    run_claude as _spawn,
)


def is_worktree() -> bool:
    """True if we're running inside a secondary git worktree."""
    result = git("rev-parse", "--git-common-dir")
    if result.returncode != 0:
        return False
    result2 = git("rev-parse", "--git-dir")
    if result2.returncode != 0:
        return False
    # In a worktree, --git-dir != --git-common-dir
    return result.stdout.strip() != result2.stdout.strip()


def is_branch_merged(branch: str, into: str = "master") -> bool:
    """Check if branch is already merged into target."""
    result = git("branch", "--merged", into)
    if result.returncode != 0:
        return False
    merged = [b.strip().lstrip("* ") for b in result.stdout.splitlines()]
    return branch in merged


def squash_merge(branch: str, message: str) -> tuple[bool, str]:
    """Squash merge branch into current branch. Returns (success, error)."""
    result = git("merge", "--squash", branch)
    if result.returncode != 0:
        # Check for merge conflict
        if "conflict" in (result.stdout + result.stderr).lower():
            git("merge", "--abort")
            return False, "merge-conflict"
        return False, result.stderr.strip()

    result = git("commit", "-m", message)
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


# There is deliberately no push() here. The runner merges to master locally
# and stops; pushing is the operator's call (review runner Bug 9 / security:
# an unattended push from the interactive checkout was the worst-case path).
MERGED_LOCALLY_NOTE = (
    "RUNNER MERGED LOCALLY: {title}. Branch {branch} squash-merged to master "
    "in the working checkout but NOT pushed — review `git log master` and push "
    "when satisfied."
)


# -- Scribe helper --

def write_scribe_note(note_type: str, content: str):
    """Write a scribe note. Warn on failure but don't abort."""
    result = subprocess.run(
        [sys.executable, str(NOTES_SCRIPT), "add", "--type", note_type, "--content", content],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        print(f"Warning: failed to write scribe note: {result.stderr.strip()}", file=sys.stderr)


# -- Deferral review --

def run_claude(prompt: str) -> tuple[int, str, str]:
    """Run Claude Code subprocess and return (returncode, stdout, stderr)."""
    return _spawn(prompt, timeout=120)


def collect_deferrals(harden: dict) -> list[dict]:
    """Collect all deferred findings with their reasons from harden rounds."""
    deferrals = []
    seen_ids = set()
    for rnd in harden.get("rounds", []):
        resolutions = rnd.get("resolutions", {})
        for rid, action in resolutions.items():
            if action == "deferred" and rid not in seen_ids:
                seen_ids.add(rid)
                finding_desc = ""
                for f in rnd.get("findings", []):
                    if f.get("id") == rid:
                        finding_desc = f.get("description", "")
                        break
                reason = ""
                for r in rnd.get("responses", []):
                    if r.get("finding_ref") == rid:
                        reason = r.get("description", "")
                        break
                deferrals.append({
                    "id": rid,
                    "finding": finding_desc,
                    "reason": reason,
                })
    return deferrals


def review_deferrals(deferrals: list[dict], title: str) -> tuple[bool, list[dict]]:
    """Review deferred findings via Claude. Returns (all_accepted, reviews)."""
    deferral_text = json.dumps(deferrals, indent=2)
    prompt = "\n".join([
        "You are a code review triage agent. Your job is to decide whether "
        "deferred findings from an adversarial code review are acceptable "
        "deferrals or need human escalation.",
        "",
        f"## Runner: {title}",
        "",
        "## Deferred findings to review",
        f"```json\n{deferral_text}\n```",
        "",
        "## Instructions",
        "",
        "For each deferral, decide:",
        "- **accept**: The deferral reason is sound — the finding is a style nit, "
        "out of scope, or the existing code is correct. Safe to auto-merge.",
        "- **escalate**: The finding points to a real risk (bug, security issue, "
        "data loss) that the defender dismissed without adequate justification.",
        "",
        "Output ONLY a JSON object:",
        '{"reviews": [{"id": "ATK-001", "decision": "accept|escalate", '
        '"rationale": "brief reason"}]}',
    ])

    try:
        rc, stdout, stderr = run_claude(prompt)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, []

    if rc != 0:
        return False, []

    text = extract_text(stdout)
    review_json = extract_json_from_text(text)

    try:
        parsed = json.loads(review_json)
        reviews = parsed.get("reviews", []) if isinstance(parsed, dict) else parsed
    except (json.JSONDecodeError, TypeError):
        return False, []

    all_accepted = all(r.get("decision") == "accept" for r in reviews)
    return all_accepted, reviews


# -- Artifact loading --

def load_artifact(path: Path, name: str, schema_version: int | None = None) -> dict:
    """Load a JSON artifact or exit with error.

    If schema_version is provided, assert the artifact declares that version.
    """
    if not path.exists():
        print(f"{name} not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid {name} JSON: {e}", file=sys.stderr)
        sys.exit(1)
    if schema_version is not None:
        try:
            assert_schema_version(data, name.lower(), schema_version)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    return data


def main():
    parser = argparse.ArgumentParser(description="Approval — runner stage 6")
    parser.add_argument("harden_result", help="Path to harden result JSON")
    args = parser.parse_args()

    # Read harden result
    harden_path = Path(args.harden_result)
    if not harden_path.exists():
        print(f"Harden result not found: {args.harden_result}", file=sys.stderr)
        sys.exit(1)

    harden = load_artifact(harden_path, "Harden result", HARDEN_SCHEMA_VERSION)
    uuid = harden.get("uuid")
    verdict = harden.get("verdict")
    branch = harden.get("branch", f"runner/{uuid}")
    unresolved = harden.get("unresolved", [])

    # Load all prior artifacts
    intake_path = intake_dir() / f"{uuid}.json"
    spec_path = specs_dir() / f"{uuid}.json"
    plan_path = plans_dir() / f"{uuid}.json"
    exec_path = executions_dir() / f"{uuid}.json"

    intake = load_artifact(intake_path, "Intake")
    spec = load_artifact(spec_path, "Spec", SPEC_SCHEMA_VERSION)
    plan = load_artifact(plan_path, "Plan", PLAN_SCHEMA_VERSION)
    execution = load_artifact(exec_path, "Execution log", EXECUTION_SCHEMA_VERSION)

    title = intake.get("title", "Untitled")

    # Gather stats
    steps = execution.get("steps", [])
    steps_passed = sum(1 for s in steps if s.get("status") == "passed")
    total_steps = len(steps)
    files_changed = iter_unique(
        f for s in steps for f in s.get("files_changed", [])
    )

    total_findings = harden.get("total_findings", 0)
    total_resolved = harden.get("total_resolved", 0)
    harden_rounds = len(harden.get("rounds", []))

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{uuid}.json"
    now = datetime.now(timezone.utc).isoformat()

    path_taken = None
    exit_code = 0

    if verdict == "all_resolved":
        # Check if already merged
        if is_branch_merged(branch):
            path_taken = "already-merged"
            print(f"Branch {branch} already merged — skipping merge", file=sys.stderr)
        elif is_worktree():
            # Running inside a secondary worktree — cannot checkout master.
            # Defer merge to manual review or next cron cycle on the main tree.
            path_taken = "worktree-deferred"
            print(f"Worktree mode: deferring merge of {branch} to master (manual merge required)", file=sys.stderr)
        else:
            # Checkout master and squash merge
            try:
                checkout("master")
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                sys.exit(1)

            # Build commit message
            body_lines = [
                f"Files changed: {len(files_changed)}",
                f"Harden rounds: {harden_rounds}",
                f"Findings resolved: {total_resolved}",
            ]
            commit_msg = f"runner/{uuid}: {title}\n\n" + "\n".join(body_lines)

            ok, err = squash_merge(branch, commit_msg)
            if not ok:
                if err == "merge-conflict":
                    path_taken = "merge-conflict"
                    print(f"Merge conflict on {branch} -- manual resolution required", file=sys.stderr)
                    write_scribe_note(
                        "todo",
                        f"RUNNER MERGE CONFLICT: {title}. Branch {branch} has merge conflicts. "
                        f"Resolve manually and merge."
                    )
                    exit_code = 1
                else:
                    path_taken = "merge-failed"
                    print(f"Merge failed: {err}", file=sys.stderr)
                    exit_code = 1
            else:
                path_taken = "merged-locally"
                print(f"Merged {branch} to master locally; not pushed", file=sys.stderr)
                write_scribe_note(
                    "todo",
                    MERGED_LOCALLY_NOTE.format(title=title, branch=branch)
                    + f" {total_resolved} findings resolved across {harden_rounds} harden rounds."
                )

    elif verdict == "defender_failed":
        # Harden run is structurally broken — defender produced no responses
        # despite attacker findings. Do not auto-merge, do not review deferrals
        # (there are none), do not write a scribe note. The reviewer sees this
        # in the runner queue via the HARDEN column and run_history.jsonl status.
        path_taken = "defender-failed"
        exit_code = 1

    elif verdict == "has_unresolved":
        # Review deferrals before deciding
        deferrals = collect_deferrals(harden)
        if deferrals:
            print(f"Reviewing {len(deferrals)} deferral(s)...", file=sys.stderr)
            all_accepted, reviews = review_deferrals(deferrals, title)

            if all_accepted and reviews:
                print(f"All deferrals accepted — upgrading to auto-merge", file=sys.stderr)
                verdict = "all_resolved"
                unresolved = []
                harden["verdict"] = verdict
                harden["unresolved"] = unresolved
                harden["deferral_reviews"] = reviews

        if verdict == "all_resolved":
            if is_branch_merged(branch):
                path_taken = "already-merged"
                print(f"Branch {branch} already merged — skipping merge", file=sys.stderr)
            elif is_worktree():
                path_taken = "worktree-deferred"
                print(f"Worktree mode: deferring merge of {branch} to master (manual merge required)", file=sys.stderr)
            else:
                try:
                    checkout("master")
                except RuntimeError as e:
                    print(str(e), file=sys.stderr)
                    sys.exit(1)

                body_lines = [
                    f"Files changed: {len(files_changed)}",
                    f"Harden rounds: {harden_rounds}",
                    f"Findings resolved: {total_resolved}",
                    f"Deferrals accepted: {len(deferrals)}",
                ]
                commit_msg = f"runner/{uuid}: {title}\n\n" + "\n".join(body_lines)

                ok, err = squash_merge(branch, commit_msg)
                if not ok:
                    if err == "merge-conflict":
                        path_taken = "merge-conflict"
                        print(f"Merge conflict on {branch} -- manual resolution required", file=sys.stderr)
                        write_scribe_note(
                            "todo",
                            f"RUNNER MERGE CONFLICT: {title}. Branch {branch} has merge conflicts. "
                            f"Resolve manually and merge."
                        )
                        exit_code = 1
                    else:
                        path_taken = "merge-failed"
                        print(f"Merge failed: {err}", file=sys.stderr)
                        exit_code = 1
                else:
                    path_taken = "merged-locally"
                    print(f"Merged {branch} to master locally; not pushed", file=sys.stderr)
                    write_scribe_note(
                        "todo",
                        MERGED_LOCALLY_NOTE.format(title=title, branch=branch)
                        + f" {total_resolved} findings resolved, {len(deferrals)} deferrals accepted "
                        f"across {harden_rounds} harden rounds."
                    )
        else:
            path_taken = "pending-review"
            unresolved_list = ", ".join(unresolved) if unresolved else "unknown"
            write_scribe_note(
                "todo",
                f"RUNNER PENDING REVIEW: {title}. Branch {branch} has "
                f"{len(unresolved)} unresolved findings: {unresolved_list}. "
                f"Review and resolve before merging."
            )

    # Build report
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "uuid": uuid,
        "title": title,
        "verdict": verdict,
        "path_taken": path_taken,
        "timestamps": {
            "intake_created": intake.get("created_at"),
            "approval_completed": now,
        },
        "stats": {
            "total_steps": total_steps,
            "steps_passed": steps_passed,
            "total_findings": total_findings,
            "findings_resolved": total_resolved,
            "findings_unresolved": len(unresolved),
            "harden_rounds": harden_rounds,
        },
        "files_changed": files_changed,
        "artifacts": {
            "intake": str(intake_path),
            "spec": str(spec_path),
            "plan": str(plan_path),
            "execution": str(exec_path),
            "harden": str(harden_path),
            "report": str(report_path),
        },
    }

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"{verdict} ({path_taken})")
    print(str(report_path))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
