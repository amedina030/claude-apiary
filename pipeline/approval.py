#!/usr/bin/env python3
"""
Approval — Stage 6 of the pipeline.

Reads the harden verdict and either:
  - all_resolved: squash merge to master, push, write decision note
  - has_unresolved: leave branch, write TODO note

Produces a final pipeline report aggregating all stage artifacts.
No LLM calls — purely mechanical.

Output: pipeline/reports/<uuid>.json + scribe note

Usage:
    approval.py <path_to_harden_result.json>
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
REPORTS_DIR = SCRIPT_DIR / "reports"
NOTES_SCRIPT = REPO_DIR / "scribe" / "notes.py"


# -- Git helpers --

def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), capture_output=True, text=True)


def get_current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def checkout(branch: str):
    result = git("checkout", branch)
    if result.returncode != 0:
        raise RuntimeError(f"Git checkout failed: {result.stderr.strip()}")


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


def push() -> tuple[bool, str]:
    """Push current branch to remote. Returns (success, error)."""
    result = git("push")
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


# -- Scribe helper --

def write_scribe_note(note_type: str, content: str):
    """Write a scribe note. Warn on failure but don't abort."""
    result = subprocess.run(
        [sys.executable, str(NOTES_SCRIPT), "add", "--type", note_type, "--content", content],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: failed to write scribe note: {result.stderr.strip()}", file=sys.stderr)


# -- Deferral review --

def run_claude(prompt: str) -> tuple[int, str, str]:
    """Run Claude Code subprocess and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["claude", "-p", "-", "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=120,
    )
    from cost_emit import emit_usage_xml
    emit_usage_xml(result.stdout)
    return result.returncode, result.stdout, result.stderr


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
    """Extract JSON from text that may contain prose or markdown fences."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", text)
    if fence_match:
        return fence_match.group(1).strip()
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        if start == -1:
            continue
        end = text.rfind(end_char)
        if end > start:
            return text[start:end + 1]
    return text


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
        f"## Pipeline: {title}",
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

def load_artifact(path: Path, name: str) -> dict:
    """Load a JSON artifact or exit with error."""
    if not path.exists():
        print(f"{name} not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid {name} JSON: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Approval — pipeline stage 6")
    parser.add_argument("harden_result", help="Path to harden result JSON")
    args = parser.parse_args()

    # Read harden result
    harden_path = Path(args.harden_result)
    if not harden_path.exists():
        print(f"Harden result not found: {args.harden_result}", file=sys.stderr)
        sys.exit(1)

    harden = load_artifact(harden_path, "Harden result")
    uuid = harden.get("uuid")
    verdict = harden.get("verdict")
    branch = harden.get("branch", f"pipeline/{uuid}")
    unresolved = harden.get("unresolved", [])

    # Load all prior artifacts
    intake_path = SCRIPT_DIR / "intake" / f"{uuid}.json"
    spec_path = SCRIPT_DIR / "specs" / f"{uuid}.json"
    plan_path = SCRIPT_DIR / "plans" / f"{uuid}.json"
    exec_path = SCRIPT_DIR / "executions" / f"{uuid}.json"

    intake = load_artifact(intake_path, "Intake")
    spec = load_artifact(spec_path, "Spec")
    plan = load_artifact(plan_path, "Plan")
    execution = load_artifact(exec_path, "Execution log")

    title = intake.get("title", "Untitled")

    # Gather stats
    steps = execution.get("steps", [])
    steps_passed = sum(1 for s in steps if s.get("status") == "passed")
    total_steps = len(steps)
    files_changed = []
    for s in steps:
        for f in s.get("files_changed", []):
            if f and f not in files_changed:
                files_changed.append(f)

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
        else:
            # Checkout master and squash merge
            original_branch = get_current_branch()
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
            commit_msg = f"pipeline/{uuid}: {title}\n\n" + "\n".join(body_lines)

            ok, err = squash_merge(branch, commit_msg)
            if not ok:
                if err == "merge-conflict":
                    path_taken = "merge-conflict"
                    print(f"Merge conflict on {branch} -- manual resolution required", file=sys.stderr)
                    write_scribe_note(
                        "todo",
                        f"PIPELINE MERGE CONFLICT: {title}. Branch {branch} has merge conflicts. "
                        f"Resolve manually and merge."
                    )
                    exit_code = 1
                else:
                    path_taken = "merge-failed"
                    print(f"Merge failed: {err}", file=sys.stderr)
                    exit_code = 1
            else:
                # Push
                ok, err = push()
                if not ok:
                    path_taken = "push-failed"
                    print(f"Push failed: {err}", file=sys.stderr)
                    write_scribe_note(
                        "todo",
                        f"PIPELINE PUSH FAILED: {title}. Branch {branch} merged locally but push failed: {err}. "
                        f"Push manually."
                    )
                    exit_code = 1
                else:
                    path_taken = "auto-merged"
                    write_scribe_note(
                        "decision",
                        f"PIPELINE COMPLETE: {title}. Auto-merged {branch} to master. "
                        f"{total_resolved} findings resolved across {harden_rounds} harden rounds."
                    )

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
            else:
                original_branch = get_current_branch()
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
                commit_msg = f"pipeline/{uuid}: {title}\n\n" + "\n".join(body_lines)

                ok, err = squash_merge(branch, commit_msg)
                if not ok:
                    if err == "merge-conflict":
                        path_taken = "merge-conflict"
                        print(f"Merge conflict on {branch} -- manual resolution required", file=sys.stderr)
                        write_scribe_note(
                            "todo",
                            f"PIPELINE MERGE CONFLICT: {title}. Branch {branch} has merge conflicts. "
                            f"Resolve manually and merge."
                        )
                        exit_code = 1
                    else:
                        path_taken = "merge-failed"
                        print(f"Merge failed: {err}", file=sys.stderr)
                        exit_code = 1
                else:
                    ok, err = push()
                    if not ok:
                        path_taken = "push-failed"
                        print(f"Push failed: {err}", file=sys.stderr)
                        write_scribe_note(
                            "todo",
                            f"PIPELINE PUSH FAILED: {title}. Branch {branch} merged locally but push failed: {err}. "
                            f"Push manually."
                        )
                        exit_code = 1
                    else:
                        path_taken = "auto-merged"
                        write_scribe_note(
                            "decision",
                            f"PIPELINE COMPLETE: {title}. Auto-merged {branch} to master. "
                            f"{total_resolved} findings resolved, {len(deferrals)} deferrals accepted "
                            f"across {harden_rounds} harden rounds."
                        )
        else:
            path_taken = "pending-review"
            unresolved_list = ", ".join(unresolved) if unresolved else "unknown"
            write_scribe_note(
                "todo",
                f"PIPELINE PENDING REVIEW: {title}. Branch {branch} has "
                f"{len(unresolved)} unresolved findings: {unresolved_list}. "
                f"Review and resolve before merging."
            )

    # Build report
    report = {
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
