#!/usr/bin/env python3
"""
Pipeline orchestrator — runs all 6 stages end-to-end.

Takes an intake file, extracts the UUID, sequences all stages, passes
artifact paths between them, stops on any stage failure.

Usage:
    python pipeline/run.py pipeline/intake/<uuid>.json
"""
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
    if len(sys.argv) < 2:
        print("Usage: python pipeline/run.py <path_to_intake.json>", file=sys.stderr)
        sys.exit(1)

    intake_path = Path(sys.argv[1])
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

    print(f"Pipeline: {intake.get('title', 'Untitled')} [{uuid}]")
    print(f"{'=' * 60}")

    total_start = time.time()
    stages_completed = 0
    final_output = ""

    try:
        for i, (name, script_name, input_key) in enumerate(STAGES, 1):
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
                if stages_completed == 1:  # validate_intake just passed
                    update_board_status(uuid, 'running')
                final_output = output
            else:
                print(f"  FAILED ({elapsed:.1f}s)")
                if output:
                    for line in output.strip().splitlines()[:5]:
                        print(f"  ! {line}")
                print(f"\n{'=' * 60}")
                print(f"FAILED at stage {i}: {name}")
                print(f"Stages completed: {stages_completed}/6")
                print(f"Total time: {time.time() - total_start:.1f}s")
                update_board_status(uuid, 'failed')
                sys.exit(1)

    except KeyboardInterrupt:
        if stages_completed < len(STAGES):
            interrupted = STAGES[stages_completed][0]
        else:
            interrupted = "(post-completion)"
        print(f"\n\nInterrupted during stage {stages_completed + 1}: {interrupted}")
        print(f"Stages completed: {stages_completed}/6")
        update_board_status(uuid, 'failed')
        sys.exit(1)

    # All stages completed
    update_board_status(uuid, 'done')
    total_elapsed = time.time() - total_start
    print(f"\n{'=' * 60}")

    # Parse verdict from approval output
    verdict_line = final_output.strip().splitlines()[0] if final_output else "unknown"
    print(f"COMPLETE: {verdict_line}")
    print(f"Stages completed: 6/6")
    print(f"Total time: {total_elapsed:.1f}s")
    print(f"Report: {artifacts['report']}")


if __name__ == "__main__":
    main()
