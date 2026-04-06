#!/usr/bin/env python3
"""
Pipeline orchestrator — runs all 6 stages end-to-end.

Takes an intake file, extracts the UUID, sequences all stages, passes
artifact paths between them, stops on any stage failure.

Usage:
    python pipeline/run.py pipeline/intake/<uuid>.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BOARD_PATH = SCRIPT_DIR / "board.md"
LOG_AGENT_COST_SCRIPT = REPO_ROOT / 'budgeter' / 'log_agent_cost.py'

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

_USAGE_RE = re.compile(r'<usage>.*?</usage>', re.DOTALL)


def log_stage_cost(stage_name: str, pipeline_uuid: str, usage_xml: str) -> None:
    """Pipe a <usage> XML block to budgeter/log_agent_cost.py. Never raises.

    pipeline_uuid is used both as session_id (one logical session per pipeline run)
    and as request_id (so 'budgeter/report.py --by-request' can sum every Claude call
    that belonged to the same pipeline run, across all stages).
    """
    if not LOG_AGENT_COST_SCRIPT.exists():
        print(f'WARN: cost logging skipped for {stage_name}: {LOG_AGENT_COST_SCRIPT} not found', file=sys.stderr)
        return
    cmd = [
        sys.executable, str(LOG_AGENT_COST_SCRIPT),
        '--session-id', pipeline_uuid,
        '--agent', f'pipeline-{stage_name}',
        '--cwd', str(REPO_ROOT),
        '--request-id', pipeline_uuid,
    ]
    try:
        result = subprocess.run(cmd, input=usage_xml, text=True, capture_output=True, timeout=30, cwd=str(REPO_ROOT))
        if result.returncode != 0:
            print(f'WARN: cost logging failed for {stage_name}: {result.stderr.strip()}', file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f'WARN: cost logging failed for {stage_name}: {e}', file=sys.stderr)


def extract_usage_block(text: str) -> str | None:
    """Return the first <usage>...</usage> block found in text, or None."""
    if not text:
        return None
    match = _USAGE_RE.search(text)
    return match.group(0) if match else None


def extract_all_usage_blocks(text: str) -> list[str]:
    """Return all <usage>...</usage> blocks found in text."""
    if not text:
        return []
    return _USAGE_RE.findall(text)


def parse_usage_fields(usage_xml: str) -> dict:
    """Parse numeric fields from a <usage> XML block. Returns dict with int values."""
    result = {}
    for tag in ('total_tokens', 'tool_uses', 'duration_ms'):
        m = re.search(rf'<{tag}>\s*(\d+)\s*</{tag}>', usage_xml)
        if m:
            try:
                result[tag] = int(m.group(1))
            except ValueError:
                result[tag] = 0
                result['_malformed'] = True
        else:
            result[tag] = 0
            if f'<{tag}>' in usage_xml:
                result['_malformed'] = True
    return result


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


def record_stage_cost(stage_name: str, pipeline_uuid: str, stdout_text: str, stderr_text: str, stage_costs: list) -> None:
    """Parse usage from stage output and log cost. Appends a status entry to stage_costs.

    Stages may make multiple Claude calls (retries, multi-step execution, harden rounds),
    each emitting a <usage> block to stderr. All blocks are summed per stage.
    """
    blocks = extract_all_usage_blocks(stdout_text or '') + extract_all_usage_blocks(stderr_text or '')
    if not blocks:
        stage_costs.append({'stage': stage_name, 'tokens': 0, 'tool_uses': 0, 'duration_ms': 0, 'status': 'no_usage'})
        return

    total_tokens = 0
    total_tool_uses = 0
    total_duration_ms = 0
    any_malformed = False
    for usage_xml in blocks:
        fields = parse_usage_fields(usage_xml)
        if fields.get('_malformed'):
            any_malformed = True
            continue
        total_tokens += fields.get('total_tokens', 0)
        total_tool_uses += fields.get('tool_uses', 0)
        total_duration_ms += fields.get('duration_ms', 0)
        # Log each call separately so the budgeter sees per-call granularity
        log_stage_cost(stage_name, pipeline_uuid, usage_xml)

    if any_malformed:
        print(f'WARN: one or more malformed <usage> blocks in stage {stage_name}', file=sys.stderr)

    status = 'logged' if total_tokens > 0 else ('malformed' if any_malformed else 'no_usage')
    stage_costs.append({'stage': stage_name, 'tokens': total_tokens, 'tool_uses': total_tool_uses, 'duration_ms': total_duration_ms, 'status': status})


def print_cost_summary(stage_costs: list) -> None:
    """Print a per-stage token cost summary."""
    if not stage_costs:
        print('Cost summary: (no stages executed)')
        return
    print('Cost summary:')
    total_tokens = 0
    any_logged = False
    for entry in stage_costs:
        stage = entry['stage']
        status = entry['status']
        if status == 'no_usage':
            print(f'  pipeline-{stage}: no usage reported')
        elif status == 'malformed':
            print(f'  pipeline-{stage}: malformed usage (counted as 0)')
        else:
            tokens = entry['tokens']
            tool_uses = entry['tool_uses']
            duration_ms = entry.get('duration_ms', 0)
            print(f'  pipeline-{stage}: {tokens} tokens ({tool_uses} tool uses, {duration_ms}ms)')
            total_tokens += tokens
            any_logged = True
    print(f'  TOTAL: {total_tokens} tokens')


def run_stage(name: str, script_path: Path, input_path: Path) -> tuple[bool, str, str, float]:
    """Run a stage subprocess. Returns (success, stdout, stderr, elapsed_seconds)."""
    if not script_path.exists():
        return False, '', f"Stage script not found: {script_path}", 0.0
    if not input_path.exists():
        return False, '', f"Stage input file not found: {input_path}", 0.0

    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), str(input_path)],
            capture_output=True, text=True, timeout=cfg("orchestrator", "stage_timeout", 3600),
        )
        elapsed = time.time() - start
        return result.returncode == 0, result.stdout or '', result.stderr or '', elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        timeout_val = cfg("orchestrator", "stage_timeout", 3600)
        return False, '', f'Stage timed out ({timeout_val}s limit)', elapsed
    except OSError as e:
        elapsed = time.time() - start
        return False, '', f'Stage failed to launch: {e}', elapsed



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
    stage_costs: list[dict] = []  # each entry: {'stage': str, 'tokens': int, 'tool_uses': int, 'status': 'logged'|'no_usage'|'malformed'}

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

            ok, stdout_text, stderr_text, elapsed = run_stage(name, script_path, input_path)
            record_stage_cost(name, uuid, stdout_text, stderr_text, stage_costs)
            output = stdout_text.strip() if ok else stderr_text.strip()

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
                    lines = output.strip().splitlines()
                    cap = 500
                    for line in lines[:cap]:
                        print(f"  ! {line}")
                    if len(lines) > cap:
                        print(f"  ! ... ({len(lines) - cap} more lines truncated)")
                print(f"\n{'=' * 60}")
                print(f"FAILED at stage {i}: {name}")
                print(f"Stages completed: {stages_completed}/{len(STAGES)}")
                print(f"Total time: {time.time() - total_start:.1f}s")
                update_board_status(uuid, 'failed')
                try:
                    print_cost_summary(stage_costs)
                except Exception:
                    pass
                print(f"To resume: python pipeline/run.py {intake_path_abs} --resume-from {name}", file=sys.stderr)  # ATK-007
                sys.exit(1)

    except KeyboardInterrupt:
        print(f"\n\nInterrupted during stage {current_stage_idx}: {current_stage_name}")  # ATK-004
        print(f"Stages completed: {stages_completed}/{len(STAGES)}")
        update_board_status(uuid, 'failed')
        print_cost_summary(stage_costs)
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
    print_cost_summary(stage_costs)


if __name__ == "__main__":
    main()
