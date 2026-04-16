#!/usr/bin/env python3
"""Integration tests for run.py --detached flow. Uses unittest.mock to stub stages."""
import io, json, os, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

# T-2026-123: flip the runner-side test-isolation guard BEFORE importing the
# runner modules so any unpatched write to production run_history.jsonl /
# overnight.jsonl raises loudly. Tests must patch RUN_HISTORY_FILE and
# OVERNIGHT_LOG to tempdir paths per-case (the TestDetachedRun.setUp
# forwards the history-file patch automatically).
os.environ["APIARY_RUNNER_TEST_ISOLATION"] = "1"

from runner import run          # noqa: E402
from runner import detached_lib  # noqa: E402
from runner import run_history as run_history_module  # noqa: E402
from runner import run_tracker as run_tracker_module  # noqa: E402
from runner import queue as runner_queue  # noqa: E402


def _make_fake_usage(tokens: int = 1000) -> str:
    return (
        f'<usage><total_tokens>{tokens}</total_tokens>'
        f'<tool_uses>2</tool_uses><duration_ms>500</duration_ms></usage>'
    )


def _make_cli_args(token_cap: int = 5_000_000, max_unreviewed: int = 5, intake=None):
    """Build a minimal namespace compatible with run_detached(cli_args)."""
    ns = mock.MagicMock()
    ns.token_cap = token_cap
    ns.max_unreviewed = max_unreviewed
    ns.intake = intake
    return ns


def _init_git_repo(root: Path) -> None:
    subprocess.run(['git', 'init', '-q', '-b', 'master'], cwd=str(root), check=True)
    subprocess.run(['git', 'config', 'user.email', 't@t'], cwd=str(root), check=True)
    subprocess.run(['git', 'config', 'user.name', 't'], cwd=str(root), check=True)
    (root / 'README').write_text('x', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=str(root), check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=str(root), check=True)


class TestDetachedRun(unittest.TestCase):

    def setUp(self):
        # prune_stale_worktrees runs inside the real apiary repo by default
        # and would mutate disk state — mock it out for every detached test.
        self._prune_patcher = mock.patch('runner.run.prune_stale_worktrees', return_value=[])
        self._prune_patcher.start()
        self.addCleanup(self._prune_patcher.stop)

        # T-2026-123: every detached-run test drives run.run_detached(),
        # which calls history_append() -> run_history.append_entry(). That
        # write defaults to runner/run_history.jsonl (the real one) unless
        # we redirect it. Patch the module global to a tempdir path for
        # every test in this class; the APIARY_RUNNER_TEST_ISOLATION guard
        # will raise if anything slips past this.
        self._history_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._history_tmp.cleanup)
        self._history_path = Path(self._history_tmp.name) / "run_history.jsonl"
        self._history_patcher = mock.patch.object(
            run_history_module, "RUN_HISTORY_FILE", self._history_path,
        )
        self._history_patcher.start()
        self.addCleanup(self._history_patcher.stop)

        # Redirect run_tracker.RUNS_DIR to a tempdir so tests don't
        # accumulate tracker state in the real runner/runs/ directory
        # and don't hit cross-run caps from previous test invocations.
        self._tracker_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tracker_tmp.cleanup)
        self._tracker_patcher = mock.patch.object(
            run_tracker_module, "RUNS_DIR",
            Path(self._tracker_tmp.name) / "runs",
        )
        self._tracker_patcher.start()
        self.addCleanup(self._tracker_patcher.stop)

    def _make_intake_file(self, directory: Path, uid: str = 'test-uuid-1234',
                          title: str = 'Test Item') -> Path:
        p = directory / f'{uid}.json'
        p.write_text(json.dumps({'id': uid, 'title': title}), encoding='utf-8')
        return p

    def _make_fake_worktree(self, td: Path, picked_path: Path) -> Path:
        """Create a worktree-shaped temp dir with runner/backlog/<picked.name>
        already in place. Tests use this in place of a real `git worktree add`
        because git_worktree_create is mocked."""
        wt = td / 'wt'
        wt_backlog = wt / 'runner' / 'backlog'
        wt_backlog.mkdir(parents=True)
        (wt_backlog / picked_path.name).write_bytes(picked_path.read_bytes())
        return wt

    def _read_log(self, log_path: Path) -> list:
        if not log_path.exists():
            return []
        return [json.loads(ln) for ln in log_path.read_text(encoding='utf-8').splitlines() if ln.strip()]

    def test_successful_detached_run(self):
        """All 6 stages pass → exit 0, log entry exit_status='ok', stages_completed=6."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', return_value=(True, '')),
                mock.patch('runner.run.run_stage', return_value=(True, _make_fake_usage(100), '', 0.1)),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 0)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['exit_status'], 'ok')
            self.assertEqual(entries[0]['stages_completed'], 6)

    def test_token_cap_exceeded(self):
        """Stage returns 1000 tokens but cap is 500 → exit 1, exit_status='token_cap_exceeded'."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', return_value=(True, '')),
                mock.patch('runner.run.run_stage', return_value=(True, _make_fake_usage(1000), '', 0.1)),
            ):
                rc = run.run_detached(_make_cli_args(token_cap=500))

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['exit_status'], 'token_cap_exceeded')

    def test_stage_failure(self):
        """Second stage fails → exit 1, exit_status starts with 'stage_failed:'."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            stage_seq = iter([
                (True, _make_fake_usage(100), '', 0.1),    # stage 1 ok
                (False, '', 'simulated stage error', 0.2),  # stage 2 fails
            ])

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', return_value=(True, '')),
                mock.patch('runner.run.run_stage', side_effect=lambda *a, **kw: next(stage_seq)),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0]['exit_status'].startswith('stage_failed:'))

    def test_hygiene_queue_full(self):
        """hygiene_precheck blocks run → exit 0, log entry contains 'queue full'."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.hygiene_precheck', return_value='queue full (5/5)'),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 0)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertIn('queue full', entries[0]['exit_status'])

    def test_backlog_empty(self):
        """No backlog items available → exit 0, log entry contains 'backlog empty'."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=None),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 0)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertIn('backlog empty', entries[0]['exit_status'])

    def test_git_setup_failed(self):
        """git_worktree_create fails → exit 1, exit_status='git_setup_failed', 0 stages."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create',
                           return_value=(False, None, 'worktree path already exists')),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['exit_status'], 'git_setup_failed')
            self.assertEqual(entries[0]['stages_completed'], 0)

    def test_path_traversal_uuid_rejected(self):
        """ATK-008: intake uuid containing path separators is rejected before any worktree is created."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            evil = self._make_intake_file(backlog, uid='..\\evil')
            log_path = td / 'overnight.jsonl'

            create_branch_calls = []

            def _track_create(*args, **kwargs):
                create_branch_calls.append(args)
                return (True, td / 'wt', '')

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=evil),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', side_effect=_track_create),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            self.assertEqual(create_branch_calls, [])  # rejected before worktree creation
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['exit_status'], 'intake_invalid_id_path')

    def test_branch_name_encodes_intake_uuid(self):
        """ATK-003/004/005: created branch name must contain the intake uuid so race-detection works."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            uid = 'abc12345-feed-face-cafe-1234567890ab'
            intake_file = self._make_intake_file(backlog, uid=uid)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            captured = {}

            def _capture(branch, *args, **kwargs):
                captured['branch'] = branch
                return (True, wt_path, '')

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', side_effect=_capture),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', return_value=(True, '')),
                mock.patch('runner.run.run_stage', return_value=(True, _make_fake_usage(50), '', 0.1)),
            ):
                run.run_detached(_make_cli_args())

            self.assertIn(uid, captured['branch'])

    def test_no_usage_abort_for_non_exempt_stage(self):
        """ATK-010: a non-exempt stage that emits no <usage> blocks aborts the run."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            stage_seq = iter([
                (True, _make_fake_usage(50), '', 0.1),  # validate_intake (exempt anyway)
                (True, '', '', 0.1),                    # auto_refine: no usage → abort
            ])

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', return_value=(True, '')),
                mock.patch('runner.run.run_stage', side_effect=lambda *a, **kw: next(stage_seq)),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['exit_status'], 'no_usage_in_stage:auto_refine')

    def test_no_usage_exempt_stage_does_not_abort(self):
        """ATK-010: validate_intake (in NO_USAGE_STAGES) emitting no usage must not abort."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            # validate_intake emits no usage; subsequent stages do.
            stage_seq = iter([
                (True, '', '', 0.1),                     # validate_intake: no usage, exempt
                (True, _make_fake_usage(10), '', 0.1),   # auto_refine
                (True, _make_fake_usage(10), '', 0.1),   # auto_plan
                (True, _make_fake_usage(10), '', 0.1),   # executor
                (True, _make_fake_usage(10), '', 0.1),   # auto_harden
                (True, _make_fake_usage(10), '', 0.1),   # approval
            ])

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', return_value=(True, '')),
                mock.patch('runner.run.run_stage', side_effect=lambda *a, **kw: next(stage_seq)),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 0)
            entries = self._read_log(log_path)
            self.assertEqual(entries[0]['exit_status'], 'ok')
            self.assertEqual(entries[0]['stages_completed'], 6)

    def test_worktree_remove_failure_propagates(self):
        """ATK-002: failure to remove the worktree on exit must surface in exit_status and rc."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', return_value=(False, 'locked worktree')),
                mock.patch('runner.run.run_stage', return_value=(True, _make_fake_usage(10), '', 0.1)),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(entries[0]['exit_status'], 'worktree_remove_failed')

    def test_interrupt_during_stage_preserves_worktree(self):
        """Cleanup handler: a SIGTERM/SIGBREAK during a stage sets
        _interrupt_requested; the loop must bail with exit_status='interrupted'
        and the finally block must PRESERVE the worktree so the operator
        can inspect the partial state (spec/plan/stage stderr etc.)."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            # Stage 1 succeeds; stage 2 simulates the signal handler firing
            # mid-flight by flipping _interrupt_requested before returning.
            def _stage_side_effect(*args, **kwargs):
                if not getattr(_stage_side_effect, 'fired', False):
                    _stage_side_effect.fired = True
                    return (True, _make_fake_usage(50), '', 0.1)
                run._interrupt_requested = True
                return (False, '', 'killed by signal handler', 0.1)

            remove_calls = []
            def _track_remove(*args, **kwargs):
                remove_calls.append(args)
                return (True, '')

            try:
                with (
                    mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                    mock.patch('runner.run.INTAKE_DIR', intake_dir),
                    mock.patch('runner.run.SCRIPT_DIR', td),
                    mock.patch('runner.run.hygiene_precheck', return_value=None),
                    mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                    mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                    mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                    mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                    mock.patch('runner.run.git_worktree_remove', side_effect=_track_remove),
                    mock.patch('runner.run.run_stage', side_effect=_stage_side_effect),
                ):
                    rc = run.run_detached(_make_cli_args())
            finally:
                run._interrupt_requested = False

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['exit_status'], 'interrupted')
            # Worktree must be preserved on failure so the operator can
            # inspect partial artifacts.
            self.assertEqual(len(remove_calls), 0)

    def test_stage_failure_preserves_worktree(self):
        """A graceful stage failure (exit_status='stage_failed:...') must
        preserve the worktree directory so the operator can read the
        spec, plan, and stage stderr that would otherwise be wiped."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            stage_seq = iter([
                (True, _make_fake_usage(100), '', 0.1),    # stage 1 ok
                (False, '', 'simulated error', 0.2),       # stage 2 fails
            ])
            remove_calls = []
            def _track_remove(*args, **kwargs):
                remove_calls.append(args)
                return (True, '')

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(True, '')),
                mock.patch('runner.run.git_worktree_remove', side_effect=_track_remove),
                mock.patch('runner.run.run_stage', side_effect=lambda *a, **kw: next(stage_seq)),
            ):
                err_buf = io.StringIO()
                with redirect_stderr(err_buf):
                    rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertTrue(entries[0]['exit_status'].startswith('stage_failed:'))
            # Worktree must NOT be removed on stage failure.
            self.assertEqual(len(remove_calls), 0)
            # T-2026-130: failure message must include the cleanup command
            # so the operator does not have to dig for the uuid.
            stderr = err_buf.getvalue()
            self.assertIn(str(wt_path), stderr)
            self.assertIn(f'--cleanup {entries[0]["uuid"]}', stderr)

    def test_commit_failure_recorded(self):
        """ATK-001: silent commit failure must be reflected in exit_status, not 'ok'."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            backlog = td / 'backlog'
            backlog.mkdir()
            intake_dir = td / 'intake'
            intake_dir.mkdir()
            intake_file = self._make_intake_file(backlog)
            wt_path = self._make_fake_worktree(td, intake_file)
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('runner.run.INTAKE_DIR', intake_dir),
                mock.patch('runner.run.SCRIPT_DIR', td),
                mock.patch('runner.run.hygiene_precheck', return_value=None),
                mock.patch('runner.run.next_eligible', return_value=None),
                mock.patch('runner.run.pick_backlog_item', return_value=intake_file),
                mock.patch('runner.run.all_backlog_items_claimed', return_value=False),
                mock.patch('runner.run.git_worktree_create', return_value=(True, wt_path, '')),
                mock.patch('runner.run.git_commit_all_in', return_value=(False, 'index lock')),
                mock.patch('runner.run.git_worktree_remove', return_value=(True, '')),
                mock.patch('runner.run.run_stage', return_value=(True, _make_fake_usage(10), '', 0.1)),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(entries[0]['exit_status'], 'commit_failed')

    def test_queue_py_handles_missing_log(self):
        """Missing overnight.jsonl + unmerged branch → exit 0, 'unknown' appears in output."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)

            with (
                mock.patch.object(runner_queue, 'OVERNIGHT_LOG', td / 'no_such.jsonl'),
                mock.patch.object(runner_queue, 'list_unmerged_runner_branches',
                                  return_value=['runner/fake-item-00000001']),
                mock.patch.object(runner_queue, 'load_ticket_summary',
                                  return_value=('unknown', '')),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = runner_queue.main()

        self.assertEqual(rc, 0)
        self.assertIn('unknown', buf.getvalue())


class TestIsolationGuards(unittest.TestCase):
    """T-2026-123: self-tests for the APIARY_RUNNER_TEST_ISOLATION guard.
    Confirms a test that forgets to redirect RUN_HISTORY_FILE /
    OVERNIGHT_LOG to a tempdir path fails loudly instead of silently
    polluting production state."""

    def test_run_history_default_path_raises(self):
        # Module env is already APIARY_RUNNER_TEST_ISOLATION=1 (set at
        # import time). We temporarily reset the patched RUN_HISTORY_FILE
        # back to the production default and confirm the guard fires.
        with mock.patch.object(
            run_history_module, "RUN_HISTORY_FILE",
            run_history_module._DEFAULT_RUN_HISTORY_FILE,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                run_history_module.append_entry({"uuid": "guard-check"})
            self.assertIn("test-isolation violation", str(ctx.exception))
            self.assertIn("run_history.jsonl", str(ctx.exception))

    def test_run_history_patched_path_allowed(self):
        # A test that HAS redirected the module global must pass cleanly.
        with tempfile.TemporaryDirectory() as td_str:
            target = Path(td_str) / "history.jsonl"
            with mock.patch.object(
                run_history_module, "RUN_HISTORY_FILE", target,
            ):
                ok = run_history_module.append_entry(
                    {"uuid": "ok-check"}, path=target,
                )
            self.assertTrue(ok)
            self.assertTrue(target.exists())

    def test_overnight_log_default_path_raises(self):
        with mock.patch.object(
            detached_lib, "OVERNIGHT_LOG", detached_lib._DEFAULT_OVERNIGHT_LOG,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                detached_lib.append_overnight_log({"uuid": "guard-check"})
            self.assertIn("test-isolation violation", str(ctx.exception))
            self.assertIn("overnight.jsonl", str(ctx.exception))


class TestRunCleanup(unittest.TestCase):
    """#245: run.py --cleanup <uuid> is the abort path — delete runner
    branch(es) and archive the execution log so the operator doesn't
    have to hand-unwind a killed/interrupted run."""

    def test_find_branches_exact_match(self):
        refs = ["runner/abc-123", "runner/def-456", "master"]
        self.assertEqual(
            run._find_runner_branches_from_refs(refs, "abc-123"),
            ["runner/abc-123"],
        )

    def test_find_branches_slug_suffix_match(self):
        # Detached naming: runner/<slug>-<uuid>
        refs = ["runner/fix-bug-abc-123", "runner/other-999"]
        self.assertEqual(
            run._find_runner_branches_from_refs(refs, "abc-123"),
            ["runner/fix-bug-abc-123"],
        )

    def test_find_branches_both_forms(self):
        refs = [
            "runner/abc-123",            # interactive form
            "runner/my-feature-abc-123", # detached form
            "runner/unrelated",
        ]
        out = run._find_runner_branches_from_refs(refs, "abc-123")
        self.assertEqual(set(out), {"runner/abc-123", "runner/my-feature-abc-123"})

    def test_find_branches_rejects_non_runner_refs(self):
        refs = ["feature/abc-123", "master", "runner/ok-abc-123"]
        self.assertEqual(
            run._find_runner_branches_from_refs(refs, "abc-123"),
            ["runner/ok-abc-123"],
        )

    def test_find_branches_no_matches(self):
        refs = ["runner/abc-123", "master"]
        self.assertEqual(
            run._find_runner_branches_from_refs(refs, "missing-uuid"),
            [],
        )

    def test_find_branches_handles_non_strings(self):
        refs = ["runner/abc-123", None, 42, "runner/ok-abc-123"]
        out = run._find_runner_branches_from_refs(refs, "abc-123")
        self.assertEqual(set(out), {"runner/abc-123", "runner/ok-abc-123"})

    def test_archive_execution_log_moves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            executions = Path(tmp) / "executions"
            executions.mkdir()
            (executions / "abc-123.json").write_text('{"uuid":"abc-123"}',
                                                     encoding="utf-8")
            archived = run._archive_execution_log(executions, "abc-123")
            self.assertIsNotNone(archived)
            self.assertTrue(archived.exists())
            self.assertFalse((executions / "abc-123.json").exists())
            self.assertEqual(archived.parent.name, "archive")

    def test_archive_execution_log_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            executions = Path(tmp) / "executions"
            executions.mkdir()
            self.assertIsNone(
                run._archive_execution_log(executions, "nope")
            )

    def test_record_stage_cost_counts_malformed(self):
        # #249: a <usage> block missing a required field becomes
        # malformed (detected via parse_usage_fields's _malformed flag)
        # and contributes to the stage's malformed_count so the run
        # summary can flag the drop.
        stage_costs = []
        good = _make_fake_usage(500)
        # Missing total_tokens value renders the block malformed.
        bad = '<usage><total_tokens></total_tokens><tool_uses>1</tool_uses><duration_ms>10</duration_ms></usage>'
        blended = good + "\n" + bad
        with mock.patch("runner.run.log_stage_cost"):
            run.record_stage_cost("executor", "uid", blended, "", stage_costs)
        self.assertEqual(stage_costs[0]["malformed_count"], 1)
        self.assertEqual(stage_costs[0]["tokens"], 500)  # good block still counts

    def test_print_cost_summary_flags_malformed_totals(self):
        stage_costs = [
            {
                "stage": "executor", "tokens": 100, "tool_uses": 1,
                "duration_ms": 10, "input_tokens": 0,
                "cache_read_tokens": 0, "cache_create_tokens": 0,
                "output_tokens": 0, "status": "logged",
                "attempts": 1, "attempts_detail": [{"attempt": 1}],
                "malformed_count": 2,
            },
            {
                "stage": "auto_harden", "tokens": 50, "tool_uses": 1,
                "duration_ms": 10, "input_tokens": 0,
                "cache_read_tokens": 0, "cache_create_tokens": 0,
                "output_tokens": 0, "status": "logged",
                "attempts": 1, "attempts_detail": [{"attempt": 1}],
                "malformed_count": 1,
            },
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            run.print_cost_summary(stage_costs)
        out = buf.getvalue()
        self.assertIn("3 malformed", out)
        self.assertIn("2 stage(s)", out)
        self.assertIn("executor", out)
        self.assertIn("auto_harden", out)

    def test_print_cost_summary_no_malformed_note_when_clean(self):
        stage_costs = [{
            "stage": "executor", "tokens": 100, "tool_uses": 1,
            "duration_ms": 10, "input_tokens": 0,
            "cache_read_tokens": 0, "cache_create_tokens": 0,
            "output_tokens": 0, "status": "logged",
            "attempts": 1, "attempts_detail": [{"attempt": 1}],
            "malformed_count": 0,
        }]
        buf = io.StringIO()
        with redirect_stdout(buf):
            run.print_cost_summary(stage_costs)
        self.assertNotIn("malformed", buf.getvalue())

    def test_record_stage_cost_tracks_per_attempt(self):
        # #248: multiple <usage> blocks in one stage stdout are parsed
        # as separate attempts with the per-attempt list preserved.
        stage_costs = []
        two_attempts = _make_fake_usage(500) + "\n" + _make_fake_usage(750)
        with mock.patch("runner.run.log_stage_cost"):
            run.record_stage_cost("executor", "uid", two_attempts, "", stage_costs)
        self.assertEqual(len(stage_costs), 1)
        entry = stage_costs[0]
        self.assertEqual(entry["tokens"], 1250)
        self.assertEqual(entry["attempts"], 2)
        self.assertEqual(len(entry["attempts_detail"]), 2)
        self.assertEqual(entry["attempts_detail"][0]["attempt"], 1)
        self.assertEqual(entry["attempts_detail"][0]["tokens"], 500)
        self.assertEqual(entry["attempts_detail"][1]["tokens"], 750)

    def test_record_stage_cost_single_attempt(self):
        stage_costs = []
        with mock.patch("runner.run.log_stage_cost"):
            run.record_stage_cost(
                "executor", "uid", _make_fake_usage(100), "", stage_costs,
            )
        self.assertEqual(stage_costs[0]["attempts"], 1)
        self.assertEqual(len(stage_costs[0]["attempts_detail"]), 1)

    def test_print_cost_summary_marks_retries(self):
        stage_costs = [{
            "stage": "executor",
            "tokens": 1250,
            "tool_uses": 4,
            "duration_ms": 1000,
            "input_tokens": 0, "cache_read_tokens": 0,
            "cache_create_tokens": 0, "output_tokens": 0,
            "status": "logged",
            "attempts": 2,
            "attempts_detail": [{"attempt": 1}, {"attempt": 2}],
        }]
        buf = io.StringIO()
        with redirect_stdout(buf):
            run.print_cost_summary(stage_costs)
        out = buf.getvalue()
        self.assertIn("[2 attempts]", out)

    def test_print_cost_summary_no_retry_tag_for_single_attempt(self):
        stage_costs = [{
            "stage": "executor",
            "tokens": 100,
            "tool_uses": 1,
            "duration_ms": 100,
            "input_tokens": 0, "cache_read_tokens": 0,
            "cache_create_tokens": 0, "output_tokens": 0,
            "status": "logged",
            "attempts": 1,
            "attempts_detail": [{"attempt": 1}],
        }]
        buf = io.StringIO()
        with redirect_stdout(buf):
            run.print_cost_summary(stage_costs)
        self.assertNotIn("attempts]", buf.getvalue())

    def test_cumulative_tokens_empty(self):
        self.assertEqual(run.cumulative_tokens([]), 0)

    def test_cumulative_tokens_sums_entries(self):
        costs = [{"tokens": 100}, {"tokens": 250}, {"other": "ignored"}]
        self.assertEqual(run.cumulative_tokens(costs), 350)

    def test_interactive_token_cap_aborts_run(self):
        """#247: interactive mode must honor --token-cap like detached mode.

        Mock run_stage so every stage returns a fake 1000-token <usage>
        block. With --token-cap 500 the run should abort after the
        first stage with exit 1.
        """
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            intake_dir = td / "intake"
            intake_dir.mkdir()
            (td / "specs").mkdir()
            (td / "plans").mkdir()
            (td / "executions").mkdir()
            (td / "hardens").mkdir()
            (td / "reports").mkdir()
            intake_file = intake_dir / "abc-123.json"
            intake_file.write_text(json.dumps({
                "id": "abc-123", "title": "t",
            }), encoding="utf-8")

            argv = [
                "run.py", str(intake_file), "--token-cap", "500",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("runner.run.SCRIPT_DIR", td),
                mock.patch("runner.run.run_stage",
                           return_value=(True, _make_fake_usage(1000), "", 0.1)),
                mock.patch("runner.run.print_cost_summary"),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    run.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_interactive_no_token_cap_does_not_abort(self):
        """If --token-cap is unset, cumulative tokens are irrelevant and
        the run should walk all 6 stages to completion."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            for sub in ("intake", "specs", "plans", "executions", "hardens", "reports"):
                (td / sub).mkdir()
            intake_file = td / "intake" / "abc-123.json"
            intake_file.write_text(json.dumps({
                "id": "abc-123", "title": "t",
            }), encoding="utf-8")

            argv = ["run.py", str(intake_file)]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("runner.run.SCRIPT_DIR", td),
                mock.patch("runner.run.run_stage",
                           return_value=(True, _make_fake_usage(1000), "", 0.1)),
                mock.patch("runner.run.print_cost_summary"),
            ):
                # main() calls sys.exit at the end of a successful run?
                # Actually no — it returns. But our assertion is just
                # 'does not raise SystemExit(1)'. Catch any SystemExit
                # and assert it's not code=1.
                try:
                    run.main()
                except SystemExit as e:
                    self.assertNotEqual(e.code, 1, "run aborted unexpectedly")

    def test_should_prune_failed_old_log(self):
        self.assertTrue(
            run._should_prune_log(
                {"status": "failed"},
                mtime_epoch=1000.0,
                cutoff_epoch=2000.0,
            )
        )

    def test_should_prune_aborted_old_log(self):
        self.assertTrue(
            run._should_prune_log(
                {"status": "aborted"}, mtime_epoch=1000.0, cutoff_epoch=2000.0,
            )
        )

    def test_should_not_prune_completed_log(self):
        self.assertFalse(
            run._should_prune_log(
                {"status": "completed"}, mtime_epoch=1000.0, cutoff_epoch=2000.0,
            )
        )

    def test_should_not_prune_recent_failed_log(self):
        # mtime > cutoff → too recent → keep
        self.assertFalse(
            run._should_prune_log(
                {"status": "failed"}, mtime_epoch=3000.0, cutoff_epoch=2000.0,
            )
        )

    def test_should_not_prune_missing_status(self):
        self.assertFalse(
            run._should_prune_log(
                {"steps": []}, mtime_epoch=1000.0, cutoff_epoch=2000.0,
            )
        )

    def test_should_not_prune_non_dict(self):
        self.assertFalse(
            run._should_prune_log(
                None, mtime_epoch=1000.0, cutoff_epoch=2000.0,
            )
        )

    def test_archive_execution_log_preserves_existing_archive(self):
        # A second cleanup for the same uuid must not clobber the first
        # archived log — the second one gets a timestamp suffix.
        with tempfile.TemporaryDirectory() as tmp:
            executions = Path(tmp) / "executions"
            (executions / "archive").mkdir(parents=True)
            (executions / "archive" / "abc-123.json").write_text(
                '{"old":true}', encoding="utf-8")
            (executions / "abc-123.json").write_text(
                '{"new":true}', encoding="utf-8")
            archived = run._archive_execution_log(executions, "abc-123")
            self.assertIsNotNone(archived)
            # The original archive file should still exist.
            self.assertTrue(
                (executions / "archive" / "abc-123.json").exists()
            )
            # The new archive file should be distinct.
            self.assertNotEqual(
                archived,
                executions / "archive" / "abc-123.json",
            )


if __name__ == '__main__':
    unittest.main()
