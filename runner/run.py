#!/usr/bin/env python3
"""
Runner orchestrator — runs all 6 stages end-to-end.

Takes an intake file, extracts the UUID, sequences all stages, passes
artifact paths between them, stops on any stage failure.

Usage:
    python -m runner.run runner/intake/<uuid>.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config_loader import get as cfg
from .detached_lib import (
    slugify, pick_backlog_item, hygiene_precheck,
    all_backlog_items_claimed, append_overnight_log,
    git_worktree_create, git_commit_all_in, git_worktree_remove,
    prune_stale_worktrees,
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

# Stage definitions: (name, module, input_artifact_key)
# input_artifact_key maps to the artifact path dict
STAGES = [
    ("validate_intake", "validate_intake", "intake"),
    ("auto_refine",     "auto_refine",     "intake"),
    ("auto_plan",       "auto_plan",       "spec"),
    ("executor",        "executor",        "plan"),
    ("auto_harden",     "auto_harden",     "execution"),
    ("approval",        "approval",        "harden"),
]

_USAGE_RE = re.compile(r'<usage>.*?</usage>', re.DOTALL)

# Cleanup-handler state. Used by run_detached and run_stage to gracefully
# tear down a run when the operator (or Task Scheduler / cron) interrupts it.
# - _active_stage_proc: the currently-running stage Popen, or None.
# - _interrupt_requested: set by the signal handler; checked by run_detached.
# - _kill_on_close_job: handle to a Windows Job Object, kept alive for the
#   process lifetime so that even a TerminateProcess on the parent (e.g.
#   Stop-ScheduledTask) takes every child stage subprocess down with it.
# - _prev_signal_handlers: previous handlers to restore after run_detached.
_active_stage_proc: subprocess.Popen | None = None
_interrupt_requested: bool = False
_kill_on_close_job = None
_prev_signal_handlers: dict = {}


def _install_kill_on_job_close():
    """Attach the current process to a Windows Job Object with the
    KILL_ON_JOB_CLOSE limit so that when this process dies — even via
    TerminateProcess from `Stop-ScheduledTask` or `taskkill /F` — every
    child process belonging to the job is killed too. No-op on non-Windows
    or if the OS rejects nesting (e.g. Task Scheduler already placed us
    in a job that disallows breakaway). Returns the job handle (caller
    must keep alive) or None.
    """
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', ctypes.c_int64),
            ('PerJobUserTimeLimit', ctypes.c_int64),
            ('LimitFlags', wintypes.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', wintypes.DWORD),
            ('Affinity', ctypes.c_void_p),
            ('PriorityClass', wintypes.DWORD),
            ('SchedulingClass', wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ('IoInfo', IO_COUNTERS),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(job)
            return None
        return job
    except OSError:
        return None


def _signal_handler(signum, _frame):
    """Mark interrupt and kill the active stage subprocess (if any).

    The main loop in run_detached checks _interrupt_requested between stages
    and bails out via the worktree-cleanup finally block. Killing the active
    stage immediately turns a long Claude call into an instant exit instead
    of waiting for the per-stage timeout.
    """
    global _interrupt_requested
    _interrupt_requested = True
    proc = _active_stage_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def _install_signal_handlers() -> None:
    """Install graceful-shutdown handlers on the main thread. Idempotent."""
    sigs = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == 'win32' and hasattr(signal, 'SIGBREAK'):
        sigs.append(signal.SIGBREAK)
    for s in sigs:
        try:
            if s not in _prev_signal_handlers:
                _prev_signal_handlers[s] = signal.signal(s, _signal_handler)
        except (ValueError, OSError):
            pass


def _restore_signal_handlers() -> None:
    for s, prev in list(_prev_signal_handlers.items()):
        try:
            signal.signal(s, prev)
        except (ValueError, OSError):
            pass
    _prev_signal_handlers.clear()


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
    malformed_count = 0  # #249: surface lost cost data in the run summary
    attempts_detail = []  # #248: per-attempt breakdown for retry visibility
    for attempt_idx, usage_xml in enumerate(blocks, 1):
        fields = parse_usage_fields(usage_xml)
        if fields.get('_malformed'):
            any_malformed = True
            malformed_count += 1
            continue
        total_tokens += fields.get('total_tokens', 0)
        total_tool_uses += fields.get('tool_uses', 0)
        total_duration_ms += fields.get('duration_ms', 0)
        total_input += fields.get('input_tokens', 0)
        total_cache_read += fields.get('cache_read_input_tokens', 0)
        total_cache_create += fields.get('cache_creation_input_tokens', 0)
        total_output += fields.get('output_tokens', 0)
        attempts_detail.append({
            'attempt': attempt_idx,
            'tokens': fields.get('total_tokens', 0),
            'tool_uses': fields.get('tool_uses', 0),
            'duration_ms': fields.get('duration_ms', 0),
        })
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
        # #248: per-attempt list lets the summary call out retry cost
        # separately from a single expensive run. The total across all
        # attempts stays in the top-level 'tokens' field for callers
        # (cumulative_tokens, etc.) that don't care about the breakdown.
        'attempts': len(attempts_detail),
        'attempts_detail': attempts_detail,
        # #249: count of <usage> blocks that couldn't be parsed. The
        # run summary flags any non-zero value so the operator knows
        # real cost data was silently discarded.
        'malformed_count': malformed_count,
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
        # #248: mark stages that required retries so a 3x-cost run is
        # visually distinct from a genuinely expensive single attempt.
        attempts = entry.get('attempts', 1)
        retry_tag = f' [{attempts} attempts]' if attempts > 1 else ''
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
            print(f'  runner-{stage}{retry_tag}: {tokens} tokens '
                  f'(weighted ~{weighted}, cache {cache_pct:.0f}%, '
                  f'{tool_uses} tool uses, {duration_ms}ms)')
            total_weighted += weighted
            total_cache_read += cache_read_t
            total_input_plus_create += input_t + cache_create_t
            total_output += output_t
        else:
            print(f'  runner-{stage}{retry_tag}: {tokens} tokens ({tool_uses} tool uses, {duration_ms}ms)')
        total_tokens += tokens
        any_logged = True
    if any_breakdown:
        fresh_total = total_input_plus_create + total_cache_read
        cache_pct = (total_cache_read * 100.0 / fresh_total) if fresh_total > 0 else 0.0
        print(f'  TOTAL: {total_tokens} tokens (weighted ~{total_weighted}, cache {cache_pct:.0f}%)')
    else:
        print(f'  TOTAL: {total_tokens} tokens')

    # #249: surface malformed-usage drops at the end of the summary so
    # the user sees that cost data is incomplete even if the stages
    # otherwise succeeded. Without this the drops were silent.
    total_malformed = sum(e.get('malformed_count', 0) for e in stage_costs)
    if total_malformed:
        affected = [e['stage'] for e in stage_costs if e.get('malformed_count', 0) > 0]
        print(
            f'  WARN: {total_malformed} malformed <usage> block(s) across '
            f'{len(affected)} stage(s) ({", ".join(affected)}) — cost data '
            f'is incomplete for those stages.'
        )


def cumulative_tokens(stage_costs: list) -> int:
    """Sum tokens across all stage cost entries."""
    return sum(entry.get('tokens', 0) for entry in stage_costs)


def run_detached(cli_args) -> int:
    """Run runner in detached (cron) mode. Returns exit code 0 or 1."""
    global _kill_on_close_job, _interrupt_requested
    _interrupt_requested = False
    if _kill_on_close_job is None:
        _kill_on_close_job = _install_kill_on_job_close()
    _install_signal_handlers()
    try:
        return _run_detached_impl(cli_args)
    finally:
        _restore_signal_handlers()


def _run_detached_impl(cli_args) -> int:
    """Body of run_detached. Split out so the wrapper can manage signal-handler
    install/restore symmetrically around the actual work."""
    token_cap = cli_args.token_cap if cli_args.token_cap is not None else cfg('detached', 'token_cap', 2000000)
    max_unreviewed = cli_args.max_unreviewed if cli_args.max_unreviewed is not None else cfg('detached', 'max_unreviewed', 5)
    start_ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'

    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'

    # Clean up any worktrees left behind by a previous hard-killed run
    # (Stop-ScheduledTask / taskkill /F). Worktrees with unmerged commits
    # are preserved so partial work isn't silently dropped.
    pruned = prune_stale_worktrees()
    for p, action in pruned:
        print(f'prune_stale_worktrees: {action} {p}', file=sys.stderr)

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

    # Create isolated worktree at .runner-worktrees/<safe-branch>/. The runner
    # operates entirely inside this directory so the operator's main checkout
    # is never disturbed (no rogue branch checkout, no untracked artifact files
    # bleeding across runs, no collision with an active interactive session).
    ok, wt_path, err = git_worktree_create(branch)
    if not ok:
        print(f'ERROR: git worktree setup failed: {err}', file=sys.stderr)
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

    stage_costs: list[dict] = []
    stages_completed = 0
    exit_status = 'ok'

    try:
        # Promote backlog item -> intake inside the worktree. The worktree
        # starts from `master`, so it has its own copy of runner/backlog/ and
        # runner/intake/. Mutations here become part of the runner branch's
        # commit; the operator's main checkout is untouched.
        wt_intake_dir = wt_path / 'runner' / 'intake'
        wt_intake_dir.mkdir(parents=True, exist_ok=True)
        wt_intake_dest = wt_intake_dir / f'{uuid}.json'
        if from_backlog:
            wt_backlog_file = wt_path / 'runner' / 'backlog' / picked_path.name
            if wt_backlog_file.exists():
                shutil.copy2(str(wt_backlog_file), str(wt_intake_dest))
                try:
                    wt_backlog_file.unlink()
                except OSError as e:
                    print(f'WARN: could not remove backlog file {wt_backlog_file}: {e}', file=sys.stderr)
            else:
                # Worktree didn't have it (e.g. picked path resolved from
                # outside the tracked backlog dir) — fall back to copying the
                # source file directly into the worktree intake dir.
                shutil.copy2(str(picked_path), str(wt_intake_dest))
        else:
            # --intake explicit path: just stage it under the worktree.
            shutil.copy2(str(picked_path), str(wt_intake_dest))

        # Build artifact paths rooted at the worktree, not SCRIPT_DIR.
        artifacts = {
            'intake':    wt_path / 'runner' / 'intake'     / f'{uuid}.json',
            'spec':      wt_path / 'runner' / 'specs'      / f'{uuid}.json',
            'plan':      wt_path / 'runner' / 'plans'      / f'{uuid}.json',
            'execution': wt_path / 'runner' / 'executions' / f'{uuid}.json',
            'harden':    wt_path / 'runner' / 'hardens'    / f'{uuid}.json',
            'report':    wt_path / 'runner' / 'reports'    / f'{uuid}.json',
        }

        for name, module_name, input_key in STAGES:
            if _interrupt_requested:
                exit_status = 'interrupted'
                break
            input_path = artifacts[input_key]

            ok, stdout_text, stderr_text, _elapsed = run_stage(name, module_name, input_path, cwd=wt_path)
            record_stage_cost(name, uuid, stdout_text, stderr_text, stage_costs)

            # If the cleanup-handler killed this stage mid-flight, surface
            # 'interrupted' rather than the downstream stage_failed/no_usage
            # codes that would otherwise mask the real cause.
            if _interrupt_requested:
                exit_status = 'interrupted'
                break

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

        # Commit work into the worktree (= the runner branch). ATK-001:
        # capture commit failure into exit_status so the log entry does not
        # falsely report 'ok' when no artifacts were committed.
        commit_msg = f'runner/{uuid}: {title}'
        commit_ok, commit_err = git_commit_all_in(wt_path, commit_msg)
        if not commit_ok:
            print(f'WARN: git commit failed: {commit_err}', file=sys.stderr)
            if exit_status == 'ok':
                exit_status = 'commit_failed'
    finally:
        # Only tear the worktree down on a clean, successful run. On any
        # failure (including 'interrupted') we keep the worktree on disk
        # so the operator can inspect specs/plans/executions/reports and
        # the stage stderr left in them. The runner branch has at least
        # one commit beyond master (the backlog->intake rename), so
        # prune_stale_worktrees will preserve it on subsequent runs.
        # Use `python -m runner.run --cleanup <uuid>` to remove it
        # manually once the failure has been diagnosed.
        if exit_status == 'ok':
            rm_ok, rm_err = git_worktree_remove(wt_path)
            if not rm_ok:
                print(f'ERROR: git worktree remove failed: {rm_err}', file=sys.stderr)
                exit_status = 'worktree_remove_failed'
        else:
            print(f'Worktree preserved for inspection: {wt_path}', file=sys.stderr)

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


def run_stage(name: str, module_name: str, input_path: Path, cwd: Path | None = None) -> tuple[bool, str, str, float]:
    """Run a stage subprocess. Returns (success, stdout, stderr, elapsed_seconds).

    `cwd` defaults to the main repo root (interactive mode). Detached mode
    passes its isolated worktree path so stages mutate worktree state, never
    the operator's main checkout.
    """
    module_file = SCRIPT_DIR / f"{module_name}.py"
    if not module_file.exists():
        return False, '', f"Stage module not found: runner.{module_name}", 0.0
    if not input_path.exists():
        return False, '', f"Stage input file not found: {input_path}", 0.0

    global _active_stage_proc
    cwd_str = str(cwd) if cwd is not None else str(REPO_ROOT)
    timeout_val = cfg("orchestrator", "stage_timeout", 3600)
    start = time.time()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", f"runner.{module_name}", str(input_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=cwd_str,
        )
    except OSError as e:
        elapsed = time.time() - start
        return False, '', f'Stage failed to launch: {e}', elapsed
    _active_stage_proc = proc
    try:
        try:
            stdout_text, stderr_text = proc.communicate(timeout=timeout_val)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            elapsed = time.time() - start
            return False, '', f'Stage timed out ({timeout_val}s limit)', elapsed
    finally:
        _active_stage_proc = None
    elapsed = time.time() - start
    return proc.returncode == 0, stdout_text or '', stderr_text or '', elapsed



def _find_runner_branches_from_refs(refs: list, uuid: str) -> list:
    """Given a list of short branch names and a runner uuid, return every
    branch that belongs to this runner run.

    The runner uses two branch naming conventions:
    - Interactive mode (executor.py main): 'runner/<uuid>'
    - Detached mode (run_detached): 'runner/<slug>-<uuid>'

    We match both by looking for any branch whose name is exactly
    'runner/<uuid>' OR ends with '-<uuid>' under the runner/ prefix.
    Pure function to keep the cleanup path unit-testable.
    """
    matches = []
    exact = f"runner/{uuid}"
    suffix = f"-{uuid}"
    for ref in refs:
        if not isinstance(ref, str):
            continue
        ref = ref.strip()
        if not ref.startswith("runner/"):
            continue
        if ref == exact or ref.endswith(suffix):
            matches.append(ref)
    return matches


def _archive_execution_log(executions_dir: Path, uuid: str) -> Path | None:
    """Move runner/executions/<uuid>.json to an archive subdir.

    Returns the archived path on success, None if there was no log to
    archive. Used by run_cleanup (#245) so operators can inspect the
    last-known state of a killed run without having to grep through
    live execution logs.
    """
    src = executions_dir / f"{uuid}.json"
    if not src.exists():
        return None
    archive_dir = executions_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{uuid}.json"
    # If an archive file already exists (rare — re-cleanup), append a
    # timestamp suffix so we never silently clobber history.
    if dest.exists():
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        dest = archive_dir / f"{uuid}.{ts}.json"
    shutil.move(str(src), str(dest))
    return dest


_PRUNE_STATUSES = frozenset({"failed", "aborted"})


def _should_prune_log(log_data: dict, mtime_epoch: float, cutoff_epoch: float) -> bool:
    """Pure predicate for --prune-failed (#246).

    A log qualifies for pruning when:
    - Its top-level 'status' is 'failed' or 'aborted' (completed runs
      are the operator's decision to keep or merge, not ours).
    - Its mtime is older than the cutoff epoch (older-than-N-days).

    Split out as a pure function so the prune policy is unit-testable
    without touching the filesystem or git.
    """
    if not isinstance(log_data, dict):
        return False
    status = log_data.get("status")
    if status not in _PRUNE_STATUSES:
        return False
    return mtime_epoch < cutoff_epoch


def run_prune_failed(days: int, dry_run: bool = False) -> int:
    """Prune old failed/aborted runner branches (#246).

    Iterates runner/executions/*.json (ignoring the archive/ subdir),
    and for every log whose status is failed/aborted AND whose mtime
    is older than `days` days, invokes run_cleanup(uuid) to delete the
    matching branch(es) and archive the log. With --dry-run, lists the
    candidates without touching anything.

    Iterating logs rather than branches means we prune cleanly even
    when the branch was already deleted (log still gets archived) and
    we skip runs whose log no longer exists (hand-cleaned already).
    """
    executions = SCRIPT_DIR / "executions"
    if not executions.exists():
        print("No executions directory — nothing to prune.")
        return 0

    cutoff = time.time() - (days * 86400)
    candidates = []
    for log_path in sorted(executions.iterdir()):
        if log_path.is_dir():
            continue  # skip archive/
        if log_path.suffix != ".json":
            continue
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        mtime = log_path.stat().st_mtime
        if _should_prune_log(data, mtime, cutoff):
            candidates.append((log_path.stem, log_path.stat().st_mtime, data.get("status")))

    if not candidates:
        print(f"No failed/aborted logs older than {days} day(s) found.")
        return 0

    for uuid, mtime, status in candidates:
        age_days = int((time.time() - mtime) / 86400)
        print(f"  {uuid}  status={status}  age={age_days}d")

    if dry_run:
        print(f"\n[dry-run] {len(candidates)} log(s) would be pruned.")
        return 0

    print(f"\nPruning {len(candidates)} stale runner run(s)...")
    failures = 0
    for uuid, _, _ in candidates:
        rc = run_cleanup(uuid)
        if rc != 0:
            failures += 1
    return 1 if failures else 0


def run_cleanup(uuid: str) -> int:
    """Abort/cleanup path for a runner run (#245).

    Finds every runner branch belonging to this uuid (both interactive
    and detached naming conventions), checks out master if HEAD is
    currently on one of them, deletes the branches, and archives the
    execution log. Returns a shell exit code.
    """
    # Find matching branches. Use for-each-ref to list runner/ refs.
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/runner/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"git for-each-ref failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    refs = result.stdout.splitlines()
    branches = _find_runner_branches_from_refs(refs, uuid)
    if not branches:
        print(f"No runner branches found for uuid '{uuid}'.", file=sys.stderr)

    # If current HEAD is on one of these branches, checkout master first.
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    current = head.stdout.strip() if head.returncode == 0 else ""
    if current in branches:
        co = subprocess.run(
            ["git", "checkout", "master"], capture_output=True, text=True,
        )
        if co.returncode != 0:
            print(
                f"Refusing to delete branches: could not checkout master: "
                f"{co.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        print(f"Checked out master (was on {current})")

    # Remove any detached worktrees tied to these branches. Required
    # because `git branch -D` refuses to delete a branch that a worktree
    # has checked out. Preserved-on-failure worktrees from run_detached
    # live under .runner-worktrees/ and must be torn down here.
    wt_removed = []
    for branch in branches:
        wt_list = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        )
        if wt_list.returncode != 0:
            break
        cur_path = None
        for line in wt_list.stdout.splitlines() + [""]:
            if line.startswith("worktree "):
                cur_path = line[len("worktree "):].strip()
            elif line.startswith("branch "):
                br = line[len("branch "):].strip()
                if br.startswith("refs/heads/"):
                    br = br[len("refs/heads/"):]
                if br == branch and cur_path:
                    r = subprocess.run(
                        ["git", "worktree", "remove", "--force", cur_path],
                        capture_output=True, text=True,
                    )
                    if r.returncode == 0:
                        wt_removed.append(cur_path)
                        print(f"Removed worktree {cur_path}")
                    else:
                        print(
                            f"Failed to remove worktree {cur_path}: "
                            f"{r.stderr.strip()}",
                            file=sys.stderr,
                        )
            elif line == "":
                cur_path = None

    # Delete each matching branch.
    deleted = []
    for branch in branches:
        d = subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True, text=True,
        )
        if d.returncode == 0:
            deleted.append(branch)
            print(f"Deleted branch {branch}")
        else:
            print(
                f"Failed to delete {branch}: {d.stderr.strip()}",
                file=sys.stderr,
            )

    # Archive the execution log if present.
    archived = _archive_execution_log(SCRIPT_DIR / "executions", uuid)
    if archived is not None:
        print(f"Archived execution log to {archived}")
    else:
        print("No execution log found to archive.")

    if not deleted and archived is None:
        return 1
    return 0


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
    parser.add_argument("--cleanup", dest="cleanup", default=None, metavar="UUID",
                        help="Abort/cleanup: delete runner branch(es) for the given uuid "
                             "and archive the execution log (#245)")
    parser.add_argument("--prune-failed", dest="prune_failed", action="store_true",
                        default=False,
                        help="Prune old failed/aborted runner branches via --older-than (#246)")
    parser.add_argument("--older-than", dest="older_than", type=int, default=7,
                        metavar="DAYS",
                        help="Age threshold in days for --prune-failed (default: 7)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=False,
                        help="List prune candidates without deleting anything")
    cli_args = parser.parse_args()

    # Cleanup mode: abort path for a killed/interrupted runner run.
    if cli_args.cleanup is not None:
        sys.exit(run_cleanup(cli_args.cleanup))

    # Prune mode: delete old failed/aborted runner runs in bulk.
    if cli_args.prune_failed:
        sys.exit(run_prune_failed(cli_args.older_than, dry_run=cli_args.dry_run))

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
        for i, (name, module_name, input_key) in enumerate(STAGES, 1):
            # Skip stages before resume point
            if resume_from is not None and not reached_resume:
                if name == resume_from:
                    reached_resume = True
                else:
                    print(f"\n[{i}/6] {name}... SKIPPED (resuming)")
                    continue  # ATK-001: skipped stages do not count as completed

            current_stage_name = name
            current_stage_idx = i
            input_path = artifacts[input_key]

            print(f"\n[{i}/6] {name}...", flush=True)

            ok, stdout_text, stderr_text, elapsed = run_stage(name, module_name, input_path)
            record_stage_cost(name, uuid, stdout_text, stderr_text, stage_costs)
            output = stdout_text.strip() if ok else stderr_text.strip()

            # #247: interactive-mode token budget. If --token-cap is set,
            # abort the run as soon as cumulative tokens exceed it, so a
            # runaway hardening loop can't keep spawning forever.
            # Detached mode already has this check (#423 region); this
            # brings parity to the interactive path.
            if cli_args.token_cap is not None:
                used = cumulative_tokens(stage_costs)
                if used > cli_args.token_cap:
                    print(
                        f"\n{'=' * 60}\n"
                        f"ABORTED: token cap exceeded at stage {i} ({name}): "
                        f"{used} > {cli_args.token_cap}",
                        file=sys.stderr,
                    )
                    print(f"Stages completed: {stages_completed}/{len(STAGES)}")
                    try:
                        print_cost_summary(stage_costs)
                    except Exception:
                        pass
                    print(
                        f"To resume: python -m runner.run {intake_path_abs} "
                        f"--resume-from {name}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

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
                print(f"To resume: python -m runner.run {intake_path_abs} --resume-from {name}", file=sys.stderr)  # ATK-007
                sys.exit(1)

    except KeyboardInterrupt:
        print(f"\n\nInterrupted during stage {current_stage_idx}: {current_stage_name}")  # ATK-004
        print(f"Stages completed: {stages_completed}/{len(STAGES)}")
        print_cost_summary(stage_costs)
        print(f"To resume: python -m runner.run {intake_path_abs} --resume-from {current_stage_name}", file=sys.stderr)  # ATK-007
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
