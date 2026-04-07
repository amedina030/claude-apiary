#!/usr/bin/env python3
"""Integration tests for run.py --detached flow. Uses unittest.mock to stub stages."""
import importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

# Ensure pipeline dir is on sys.path so run and detached_lib can be imported
_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import run          # noqa: E402  (path manipulation above is intentional)
import detached_lib  # noqa: E402


def _load_pipeline_queue():
    """Load pipeline/queue.py without shadowing the stdlib queue module."""
    spec = importlib.util.spec_from_file_location('pipeline_queue', _PIPELINE_DIR / 'queue.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    def _make_intake_file(self, directory: Path, uid: str = 'test-uuid-1234',
                          title: str = 'Test Item') -> Path:
        p = directory / f'{uid}.json'
        p.write_text(json.dumps({'id': uid, 'title': title}), encoding='utf-8')
        return p

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
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('run.INTAKE_DIR', intake_dir),
                mock.patch('run.SCRIPT_DIR', td),
                mock.patch('run.hygiene_precheck', return_value=None),
                mock.patch('run.pick_backlog_item', return_value=intake_file),
                mock.patch('run.all_backlog_items_claimed', return_value=False),
                mock.patch('run.git_create_branch', return_value=(True, '')),
                mock.patch('run.git_commit_all', return_value=(True, '')),
                mock.patch('run.git_checkout', return_value=(True, '')),
                mock.patch('run.run_stage', return_value=(True, _make_fake_usage(100), '', 0.1)),
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
            log_path = td / 'overnight.jsonl'

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('run.INTAKE_DIR', intake_dir),
                mock.patch('run.SCRIPT_DIR', td),
                mock.patch('run.hygiene_precheck', return_value=None),
                mock.patch('run.pick_backlog_item', return_value=intake_file),
                mock.patch('run.all_backlog_items_claimed', return_value=False),
                mock.patch('run.git_create_branch', return_value=(True, '')),
                mock.patch('run.git_commit_all', return_value=(True, '')),
                mock.patch('run.git_checkout', return_value=(True, '')),
                mock.patch('run.run_stage', return_value=(True, _make_fake_usage(1000), '', 0.1)),
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
            log_path = td / 'overnight.jsonl'

            stage_seq = iter([
                (True, _make_fake_usage(100), '', 0.1),    # stage 1 ok
                (False, '', 'simulated stage error', 0.2),  # stage 2 fails
            ])

            with (
                mock.patch.object(detached_lib, 'OVERNIGHT_LOG', log_path),
                mock.patch('run.INTAKE_DIR', intake_dir),
                mock.patch('run.SCRIPT_DIR', td),
                mock.patch('run.hygiene_precheck', return_value=None),
                mock.patch('run.pick_backlog_item', return_value=intake_file),
                mock.patch('run.all_backlog_items_claimed', return_value=False),
                mock.patch('run.git_create_branch', return_value=(True, '')),
                mock.patch('run.git_commit_all', return_value=(True, '')),
                mock.patch('run.git_checkout', return_value=(True, '')),
                mock.patch('run.run_stage', side_effect=lambda *_: next(stage_seq)),
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
                mock.patch('run.hygiene_precheck', return_value='queue full (5/5)'),
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
                mock.patch('run.hygiene_precheck', return_value=None),
                mock.patch('run.pick_backlog_item', return_value=None),
                mock.patch('run.all_backlog_items_claimed', return_value=False),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 0)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertIn('backlog empty', entries[0]['exit_status'])

    def test_git_setup_failed(self):
        """git_create_branch fails (dirty working tree) → exit 1, exit_status='git_setup_failed', 0 stages."""
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
                mock.patch('run.INTAKE_DIR', intake_dir),
                mock.patch('run.SCRIPT_DIR', td),
                mock.patch('run.hygiene_precheck', return_value=None),
                mock.patch('run.pick_backlog_item', return_value=intake_file),
                mock.patch('run.all_backlog_items_claimed', return_value=False),
                mock.patch('run.git_create_branch',
                           return_value=(False, 'working tree has uncommitted changes')),
            ):
                rc = run.run_detached(_make_cli_args())

            self.assertEqual(rc, 1)
            entries = self._read_log(log_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['exit_status'], 'git_setup_failed')
            self.assertEqual(entries[0]['stages_completed'], 0)

    def test_queue_py_handles_missing_log(self):
        """Missing overnight.jsonl + unmerged branch → exit 0, 'unknown' appears in output."""
        pipeline_queue = _load_pipeline_queue()

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)

            with (
                mock.patch.object(pipeline_queue, 'OVERNIGHT_LOG', td / 'no_such.jsonl'),
                mock.patch.object(pipeline_queue, 'list_unmerged_pipeline_branches',
                                  return_value=['pipeline/fake-item-00000001']),
                mock.patch.object(pipeline_queue, 'load_ticket_summary',
                                  return_value=('unknown', '')),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = pipeline_queue.main()

        self.assertEqual(rc, 0)
        self.assertIn('unknown', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
