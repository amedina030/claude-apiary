#!/usr/bin/env python3
"""
Pipeline orchestrator — runs all 6 stages end-to-end.

Takes an intake file, extracts the UUID, sequences all stages, passes
artifact paths between them, stops on any stage failure.

Usage:
    python pipeline/run.py pipeline/intake/<uuid>.json
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
BOARD_PATH = SCRIPT_DIR / "board.md"

# Stage definitions: (name, script, input_artifact_key)
# input_artifact_key maps to the artifact path dict
STAGES = [
    ("validate_intake", "validate_intake.py", "intake"),
    ("auto_refine",     "auto_refine.py",     "intake"),
    ("auto_plan",       "auto_plan.py",       "spec"),
    ("executor",        "executor.py",        "plan"),
    ("auto_harden",     "auto_harden.py",     "execution"),
    ("approval",        "approval.py",        "harden"),
]


def update_board_status(intake_uuid: str, new_status: str) -> None:
    """Update board.md row matching intake_uuid to new_status. Silently skips if board.md missing or no matching row."""
    if not BOARD_PATH.exists():
        return
    try:
        text = BOARD_PATH.read_text(encoding='utf-8')
    except OSError:
        return
    lines = text.splitlines(keepends=True)
    updated = False
    for i, line in enumerate(lines):
        # Table row format: | slug | title | status | uuid | notes |
        # split('|') gives: ['', ' slug ', ' title ', ' status ', ' uuid ', ' notes ', '']
        cols = line.split('|')
        if len(cols) >= 6:
            uuid_col = cols[4].strip()
            if uuid_col == intake_uuid:
                cols[3] = f' {new_status} '
                lines[i] = '|'.join(cols)
                updated = True
                break
    if updated:
        try:
            BOARD_PATH.write_text(''.join(lines), encoding='utf-8')
        except OSError:
            pass  # Board update must not block pipeline


def run_stage(name: str, script_path: Path, input_path: Path) -> tuple[bool, str, float]:
    """Run a stage subprocess. Returns (success, output, elapsed_seconds).
    On success, output is stdout. On failure, output is stderr."""
    if not script_path.exists():
        return False, f"Stage script not found: {script_path}", 0.0
    if not input_path.exists():
        return False, f"Stage input file not found: {input_path}", 0.0

    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), str(input_path)],
            capture_output=True, text=True, timeout=cfg("orchestrator", "stage_timeout", 3600),
        )
        elapsed = time.time() - start
        if result.returncode != 0:
            return False, result.stderr.strip(), elapsed
        return True, result.stdout.strip(), elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return False, "Stage timed out (60 min limit)", elapsed
    except OSError as e:
        elapsed = time.time() - start
        return False, f"Stage failed to launch: {e}", elapsed



def main():
    parser = argparse.ArgumentParser(description="Pipeline orchestrator")
    parser.add_argument("intake", help="Path to intake JSON file")
    parser.add_argument("--resume-from", dest="resume_from", default=None,
                        help="Resume from a specific stage (skip earlier stages)")
    cli_args = parser.parse_args()

    # Validate --resume-from against known stage names
    stage_names = [name for name, _, _ in STAGES]
    if cli_args.resume_from is not None and cli_args.resume_from not in stage_names:
        print(f"Unknown stage: {cli_args.resume_from}. Valid stages: {', '.join(stage_names)}", file=sys.stderr)
        sys.exit(1)

    intake_path = Path(cli_args.intake)
    if not intake_path.exists():
        print(f"Intake file not found: {intake_path}", file=sys.stderr)
        sys.exit(1)

    # Read intake to get UUID
    try:
        intake = json.loads(intake_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid intake JSON: {e}", file=sys.stderr)
        sys.exit(1)

    uuid = intake.get("id")
    # Reject non-string ids (integers, lists, etc.) — only str is a valid id type
    if not isinstance(uuid, str):
        print("Intake file missing id field", file=sys.stderr)
        sys.exit(1)
    uuid = uuid.strip()
    if not uuid:
        print("Intake file missing id field", file=sys.stderr)
        sys.exit(1)

    # Reject ids with path separators, null bytes, or dot-only names to prevent
    # path traversal in artifact paths.
    # - backslash: on POSIX, Path("foo\\bar").name == "foo\\bar" so Path comparison alone misses it
    # - null bytes: truncate filenames on some filesystems
    # - "." / "..": Path(".").name == "." on POSIX so Path comparison alone misses them
    if (
        "\\" in uuid
        or "\x00" in uuid
        or uuid in (".", "..")
        or Path(uuid) != Path(Path(uuid).name)
        or not Path(uuid).name
    ):
        print("Intake id field contains invalid characters (path separators not allowed)", file=sys.stderr)
        sys.exit(1)

    # Derive artifact paths
    artifacts = {
        "intake":    SCRIPT_DIR / "intake"     / f"{uuid}.json",
        "spec":      SCRIPT_DIR / "specs"      / f"{uuid}.json",
        "plan":      SCRIPT_DIR / "plans"      / f"{uuid}.json",
        "execution": SCRIPT_DIR / "executions" / f"{uuid}.json",
        "harden":    SCRIPT_DIR / "hardens"    / f"{uuid}.json",
        "report":    SCRIPT_DIR / "reports"    / f"{uuid}.json",
    }

    # Verify intake path matches expected
    if intake_path.resolve() != artifacts["intake"].resolve():
        # Copy or just use the provided path — use provided path for validate,
        # but derived paths for subsequent stages
        artifacts["intake"] = intake_path.resolve()

    resume_from = cli_args.resume_from

    # Validate prerequisite artifact exists when resuming
    if resume_from is not None:
        # Find the input_key for the resume stage
        resume_input_key = None
        for name, _, input_key in STAGES:
            if name == resume_from:
                resume_input_key = input_key
                break
        artifact_path = artifacts[resume_input_key]
        if not artifact_path.exists():
            print(f"Cannot resume from {resume_from}: missing prerequisite artifact {artifact_path}", file=sys.stderr)
            sys.exit(1)

    print(f"Pipeline: {intake.get('title', 'Untitled')} [{uuid}]")
    print(f"{'=' * 60}")

    total_start = time.time()
    stages_completed = 0
    final_output = ""
    reached_resume = (resume_from is None)
    # Track the currently-executing stage for KeyboardInterrupt reporting
    current_stage_name = STAGES[0][0]
    current_stage_idx = 1
    intake_path_abs = intake_path.resolve()

    try:
        for i, (name, script_name, input_key) in enumerate(STAGES, 1):
            # Skip stages before resume point
            if resume_from is not None and not reached_resume:
                if name == resume_from:
                    reached_resume = True
                else:
                    print(f"\n[{i}/6] {name}... SKIPPED (resuming)")
                    continue  # ATK-001: skipped stages do not count as completed

            current_stage_name = name
            current_stage_idx = i
            script_path = SCRIPT_DIR / script_name
            input_path = artifacts[input_key]

            print(f"\n[{i}/6] {name}...", flush=True)

            ok, output, elapsed = run_stage(name, script_path, input_path)

            if ok:
                print(f"  PASSED ({elapsed:.1f}s)")
                if output:
                    # Show last line of output (usually the file path or verdict)
                    last_line = output.strip().splitlines()[-1]
                    print(f"  -> {last_line}")
                stages_completed += 1
                if name == "validate_intake":  # ATK-003: check stage name, not count
                    update_board_status(uuid, 'running')
                final_output = output
            else:
                print(f"  FAILED ({elapsed:.1f}s)")
                if output:
                    for line in output.strip().splitlines()[:5]:
                        print(f"  ! {line}")
                print(f"\n{'=' * 60}")
                print(f"FAILED at stage {i}: {name}")
                print(f"Stages completed: {stages_completed}/{len(STAGES)}")
                print(f"Total time: {time.time() - total_start:.1f}s")
                update_board_status(uuid, 'failed')
                print(f"To resume: python pipeline/run.py {intake_path_abs} --resume-from {name}", file=sys.stderr)  # ATK-007
                sys.exit(1)

    except KeyboardInterrupt:
        print(f"\n\nInterrupted during stage {current_stage_idx}: {current_stage_name}")  # ATK-004
        print(f"Stages completed: {stages_completed}/{len(STAGES)}")
        update_board_status(uuid, 'failed')
        print(f"To resume: python pipeline/run.py {intake_path_abs} --resume-from {current_stage_name}", file=sys.stderr)  # ATK-007
        sys.exit(1)

    # All stages completed
    update_board_status(uuid, 'done')
    total_elapsed = time.time() - total_start
    print(f"\n{'=' * 60}")

    # Parse verdict from approval output
    verdict_line = final_output.strip().splitlines()[0] if final_output else "unknown"
    print(f"COMPLETE: {verdict_line}")
    print(f"Stages completed: {stages_completed}/{len(STAGES)}")
    print(f"Total time: {total_elapsed:.1f}s")
    print(f"Report: {artifacts['report']}")


if __name__ == "__main__":
    main()
