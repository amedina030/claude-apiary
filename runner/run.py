#!/usr/bin/env python3
"""
Runner orchestrator — runs all 6 stages end-to-end.

Takes an intake file, extracts the UUID, sequences all stages, passes
artifact paths between them, stops on any stage failure.

Usage:
    python runner/run.py runner/intake/<uuid>.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config_loader import get as cfg
from detached_lib import (
    slugify, pick_backlog_item, hygiene_precheck,
    all_backlog_items_claimed, append_overnight_log,
    git_create_branch, git_commit_all, git_checkout,
    OVERNIGHT_LOG, BACKLOG_DIR, INTAKE_DIR,
)

# Stages that legitimately make no Claude calls and so always emit zero
# <usage> blocks. Used by run_detached's no_usage safety check (ATK-010):
# any other stage producing no_usage in detached mode is treated as a
# token-accounting failure and aborts the run, since cumulative tokens
# would otherwise stay 0 forever and bypass the cap.
NO_USAGE_STAGES = frozenset({'validate_intake'})

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
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


def log_stage_cost(stage_name: str, runner_uuid: str, usage_xml: str) -> None:
    """Pipe a <usage> XML block to budgeter/log_agent_cost.py. Never raises.

    runner_uuid is used both as session_id (one logical session per runner run)
    and as request_id (so 'budgeter/report.py --by-request' can sum every Claude call
    that belonged to the same runner run, across all stages).
    """
    if not LOG_AGENT_COST_SCRIPT.exists():
        print(f'WARN: cost logging skipped for {stage_name}: {LOG_AGENT_COST_SCRIPT} not found', file=sys.stderr)
        return
    cmd = [
        sys.executable, str(LOG_AGENT_COST_SCRIPT),
        '--session-id', runner_uuid,
        '--agent', f'runner-{stage_name}',
        '--cwd', str(REPO_ROOT),
        '--request-id', runner_uuid,
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
    """Parse numeric fields from a <usage> XML block. Returns dict with int values.

    Parses both the required legacy fields (total_tokens/tool_uses/duration_ms)
    and the optional per-category breakdown (input/cache_read/cache_creation/
    output) emitted by runner/cost_emit.py. Missing optional fields default
    to 0 without setting _malformed — only missing legacy fields are flagged.
    """
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
    for tag in ('input_tokens', 'cache_read_input_tokens',
                'cache_creation_input_tokens', 'output_tokens'):
        m = re.search(rf'<{tag}>\s*(\d+)\s*</{tag}>', usage_xml)
        if m:
            try:
                result[tag] = int(m.group(1))
            except ValueError:
                result[tag] = 0
        else:
            result[tag] = 0
    return result


def record_stage_cost(stage_name: str, runner_uuid: str, stdout_text: str, stderr_text: str, stage_costs: list) -> None:
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
    total_input = 0
    total_cache_read = 0
    total_cache_create = 0
    total_output = 0
    any_malformed = False
    for usage_xml in blocks:
        fields = parse_usage_fields(usage_xml)
        if fields.get('_malformed'):
            any_malformed = True
            continue
        total_tokens += fields.get('total_tokens', 0)
        total_tool_uses += fields.get('tool_uses', 0)
        total_duration_ms += fields.get('duration_ms', 0)
        total_input += fields.get('input_tokens', 0)
        total_cache_read += fields.get('cache_read_input_tokens', 0)
        total_cache_create += fields.get('cache_creation_input_tokens', 0)
        total_output += fields.get('output_tokens', 0)
        # Log each call separately so the budgeter sees per-call granularity
        log_stage_cost(stage_name, runner_uuid, usage_xml)

    if any_malformed:
        print(f'WARN: one or more malformed <usage> blocks in stage {stage_name}', file=sys.stderr)

    status = 'logged' if total_tokens > 0 else ('malformed' if any_malformed else 'no_usage')
    stage_costs.append({
        'stage': stage_name,
        'tokens': total_tokens,
        'tool_uses': total_tool_uses,
        'duration_ms': total_duration_ms,
        'input_tokens': total_input,
        'cache_read_tokens': total_cache_read,
        'cache_create_tokens': total_cache_create,
        'output_tokens': total_output,
        'status': status,
    })


def print_cost_summary(stage_costs: list) -> None:
    """Print a per-stage token cost summary.

    When per-category breakdown is present (new cost_emit.py format), also
    prints the cache-read fraction and a weighted token estimate using the
    same input=1.0 / cache=0.1 / output=5.0 weights as budgeter/report.py.
    """
    if not stage_costs:
        print('Cost summary: (no stages executed)')
        return
    print('Cost summary:')
    total_tokens = 0
    total_weighted = 0
    total_cache_read = 0
    total_input_plus_create = 0
    total_output = 0
    any_logged = False
    any_breakdown = False
    for entry in stage_costs:
        stage = entry['stage']
        status = entry['status']
        if status == 'no_usage':
            print(f'  runner-{stage}: no usage reported')
            continue
        if status == 'malformed':
            print(f'  runner-{stage}: malformed usage (counted as 0)')
            continue
        tokens = entry['tokens']
        tool_uses = entry['tool_uses']
        duration_ms = entry.get('duration_ms', 0)
        input_t = entry.get('input_tokens', 0)
        cache_read_t = entry.get('cache_read_tokens', 0)
        cache_create_t = entry.get('cache_create_tokens', 0)
        output_t = entry.get('output_tokens', 0)
        has_breakdown = (input_t + cache_read_t + cache_create_t + output_t) > 0
        if has_breakdown:
            any_breakdown = True
            # Match budgeter/report.py weights: input=1.0, cache=0.1, output=5.0.
            # Cache-creation behaves like fresh input for cap accounting.
            weighted = int((input_t + cache_create_t) * 1.0
                           + cache_read_t * 0.1
                           + output_t * 5.0)
            fresh_in = input_t + cache_read_t + cache_create_t
            cache_pct = (cache_read_t * 100.0 / fresh_in) if fresh_in > 0 else 0.0
            print(f'  runner-{stage}: {tokens} tokens '
                  f'(weighted ~{weighted}, cache {cache_pct:.0f}%, '
                  f'{tool_uses} tool uses, {duration_ms}ms)')
            total_weighted += weighted
            total_cache_read += cache_read_t
            total_input_plus_create += input_t + cache_create_t
            total_output += output_t
        else:
            print(f'  runner-{stage}: {tokens} tokens ({tool_uses} tool uses, {duration_ms}ms)')
        total_tokens += tokens
        any_logged = True
    if any_breakdown:
        fresh_total = total_input_plus_create + total_cache_read
        cache_pct = (total_cache_read * 100.0 / fresh_total) if fresh_total > 0 else 0.0
        print(f'  TOTAL: {total_tokens} tokens (weighted ~{total_weighted}, cache {cache_pct:.0f}%)')
    else:
        print(f'  TOTAL: {total_tokens} tokens')


def cumulative_tokens(stage_costs: list) -> int:
    """Sum tokens across all stage cost entries."""
    return sum(entry.get('tokens', 0) for entry in stage_costs)


def run_detached(cli_args) -> int:
    """Run runner in detached (cron) mode. Returns exit code 0 or 1."""
    token_cap = cli_args.token_cap if cli_args.token_cap is not None else cfg('detached', 'token_cap', 2000000)
    max_unreviewed = cli_args.max_unreviewed if cli_args.max_unreviewed is not None else cfg('detached', 'max_unreviewed', 5)
    start_ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'

    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'

    # Hygiene precheck
    reason = hygiene_precheck(max_unreviewed)
    if reason:
        append_overnight_log({
            'start_ts': start_ts,
            'end_ts': _now(),
            'exit_status': f'skipped: {reason}',
            'stages_completed': 0,
            'total_tokens': 0,
            'uuid': None,
            'slug': None,
            'branch': None,
        })
        return 0

    # Resolve intake path
    if cli_args.intake is not None:
        picked_path = Path(cli_args.intake)
        from_backlog = False
    else:
        picked_path = pick_backlog_item()
        from_backlog = True
        if picked_path is None:
            reason = 'all in progress' if all_backlog_items_claimed() else 'backlog empty'
            append_overnight_log({
                'start_ts': start_ts,
                'end_ts': _now(),
                'exit_status': f'skipped: {reason}',
                'stages_completed': 0,
                'total_tokens': 0,
                'uuid': None,
                'slug': None,
                'branch': None,
            })
            return 0

    # Load intake JSON
    try:
        intake = json.loads(picked_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        print(f'ERROR: could not read intake file {picked_path}: {e}', file=sys.stderr)
        append_overnight_log({
            'start_ts': start_ts,
            'end_ts': _now(),
            'exit_status': 'intake_read_failed',
            'stages_completed': 0,
            'total_tokens': 0,
            'uuid': None,
            'slug': None,
            'branch': None,
        })
        return 1

    uuid = intake.get('id')
    if not isinstance(uuid, str):
        uuid = None
    if uuid:
        uuid = uuid.strip()
    if not uuid:
        print('ERROR: intake file missing id field', file=sys.stderr)
        append_overnight_log({
            'start_ts': start_ts,
            'end_ts': _now(),
            'exit_status': 'intake_invalid_id',
            'stages_completed': 0,
            'total_tokens': 0,
            'uuid': None,
            'slug': None,
            'branch': None,
        })
        return 1

    # ATK-008: reject path-traversal characters in uuid before it is interpolated
    # into intake_dest and artifact paths. Mirrors the guard in interactive main().
    if (
        '\\' in uuid
        or '\x00' in uuid
        or uuid in ('.', '..')
        or Path(uuid) != Path(Path(uuid).name)
        or not Path(uuid).name
    ):
        print('ERROR: intake id field contains invalid characters (path separators not allowed)', file=sys.stderr)
        append_overnight_log({
            'start_ts': start_ts,
            'end_ts': _now(),
            'exit_status': 'intake_invalid_id_path',
            'stages_completed': 0,
            'total_tokens': 0,
            'uuid': None,
            'slug': None,
            'branch': None,
        })
        return 1

    title = intake.get('title', 'Untitled')
    slug = slugify(title)
    # ATK-003/004/005: encode the full intake uuid in the branch name so
    # _branch_exists_for_uuid's substring match actually works. Without this,
    # pick_backlog_item / all_backlog_items_claimed could not detect that an
    # item is already claimed by an in-flight branch.
    branch = f'runner/{slug}-{uuid}'

    # Create branch
    ok, err = git_create_branch(branch)
    if not ok:
        print(f'ERROR: git branch setup failed: {err}', file=sys.stderr)
        append_overnight_log({
            'start_ts': start_ts,
            'end_ts': _now(),
            'exit_status': 'git_setup_failed',
            'stderr': err,
            'uuid': uuid,
            'slug': slug,
            'branch': None,
            'stages_completed': 0,
            'total_tokens': 0,
        })
        return 1

    # Move backlog item to intake dir
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    intake_dest = INTAKE_DIR / f'{uuid}.json'
    if from_backlog and picked_path.resolve() != intake_dest.resolve():
        shutil.copy2(str(picked_path), str(intake_dest))
        try:
            picked_path.unlink()
        except OSError as e:
            print(f'WARN: could not remove backlog file {picked_path}: {e}', file=sys.stderr)

    # Build artifacts dict
    artifacts = {
        'intake':    SCRIPT_DIR / 'intake'     / f'{uuid}.json',
        'spec':      SCRIPT_DIR / 'specs'      / f'{uuid}.json',
        'plan':      SCRIPT_DIR / 'plans'      / f'{uuid}.json',
        'execution': SCRIPT_DIR / 'executions' / f'{uuid}.json',
        'harden':    SCRIPT_DIR / 'hardens'    / f'{uuid}.json',
        'report':    SCRIPT_DIR / 'reports'    / f'{uuid}.json',
    }

    stage_costs: list[dict] = []
    stages_completed = 0
    exit_status = 'ok'

    for name, script_name, input_key in STAGES:
        script_path = SCRIPT_DIR / script_name
        input_path = artifacts[input_key]

        ok, stdout_text, stderr_text, _elapsed = run_stage(name, script_path, input_path)
        record_stage_cost(name, uuid, stdout_text, stderr_text, stage_costs)

        # ATK-010: in detached mode the cap is the only safety against runaway
        # cost. A stage that emits zero <usage> blocks would leave cumulative
        # tokens at 0 forever, defeating the cap. Stages in NO_USAGE_STAGES are
        # known to make no Claude calls; for any other stage, no_usage means
        # token accounting is broken and we must abort fail-closed.
        last = stage_costs[-1] if stage_costs else None
        if (
            ok
            and last is not None
            and last.get('status') == 'no_usage'
            and name not in NO_USAGE_STAGES
        ):
            exit_status = f'no_usage_in_stage:{name}'
            break

        if cumulative_tokens(stage_costs) > token_cap:
            exit_status = 'token_cap_exceeded'
            break

        if not ok:
            exit_status = f'stage_failed:{name}'
            break

        stages_completed += 1

    # Commit work. ATK-001: capture commit failure into exit_status so the log
    # entry does not falsely report 'ok' when no artifacts were committed.
    commit_msg = f'runner/{uuid}: {title}'
    commit_ok, commit_err = git_commit_all(commit_msg)
    if not commit_ok:
        print(f'WARN: git commit failed: {commit_err}', file=sys.stderr)
        if exit_status == 'ok':
            exit_status = 'commit_failed'

    # ATK-002: must_not_break requires that cron always restore master on exit.
    # If the checkout fails, capture it in the entry's exit_status (so morning
    # review surfaces it) and return non-zero so cron sees the failure.
    checkout_ok, checkout_err = git_checkout('master')
    if not checkout_ok:
        print(f'ERROR: git checkout master failed: {checkout_err}', file=sys.stderr)
        if exit_status == 'ok':
            exit_status = 'checkout_master_failed'
        else:
            exit_status = f'{exit_status}+checkout_master_failed'

    end_ts = _now()
    entry = {
        'start_ts': start_ts,
        'end_ts': end_ts,
        'uuid': uuid,
        'slug': slug,
        'branch': branch,
        'stages_completed': stages_completed,
        'total_tokens': cumulative_tokens(stage_costs),
        'exit_status': exit_status,
    }
    append_overnight_log(entry)

    return 0 if exit_status == 'ok' else 1


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
    parser = argparse.ArgumentParser(description="Runner orchestrator")
    parser.add_argument("intake", nargs='?', default=None, help="Path to intake JSON file")
    parser.add_argument("--resume-from", dest="resume_from", default=None,
                        help="Resume from a specific stage (skip earlier stages)")
    parser.add_argument("--detached", action="store_true", default=False,
                        help="Run in detached (cron) mode: pick from backlog, branch, commit, log")
    parser.add_argument("--token-cap", dest="token_cap", type=int, default=None,
                        help="Per-run token cap (detached mode); defaults to config detached.token_cap")
    parser.add_argument("--max-unreviewed", dest="max_unreviewed", type=int, default=None,
                        help="Max unmerged runner branches before skipping (detached mode)")
    cli_args = parser.parse_args()

    # Detached mode: hand off entirely, then exit
    if cli_args.detached:
        sys.exit(run_detached(cli_args))

    # Validate --resume-from against known stage names
    stage_names = [name for name, _, _ in STAGES]
    if cli_args.resume_from is not None and cli_args.resume_from not in stage_names:
        print(f"Unknown stage: {cli_args.resume_from}. Valid stages: {', '.join(stage_names)}", file=sys.stderr)
        sys.exit(1)

    if cli_args.intake is None:
        print("intake path is required in interactive mode (use --detached for cron mode)", file=sys.stderr)
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

    print(f"Runner: {intake.get('title', 'Untitled')} [{uuid}]")
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
                try:
                    print_cost_summary(stage_costs)
                except Exception:
                    pass
                print(f"To resume: python runner/run.py {intake_path_abs} --resume-from {name}", file=sys.stderr)  # ATK-007
                sys.exit(1)

    except KeyboardInterrupt:
        print(f"\n\nInterrupted during stage {current_stage_idx}: {current_stage_name}")  # ATK-004
        print(f"Stages completed: {stages_completed}/{len(STAGES)}")
        print_cost_summary(stage_costs)
        print(f"To resume: python runner/run.py {intake_path_abs} --resume-from {current_stage_name}", file=sys.stderr)  # ATK-007
        sys.exit(1)

    # All stages completed
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
