#!/usr/bin/env python3
"""Helpers for the detached cron-driven runner mode."""
from __future__ import annotations
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .target_repo import (
    backlog_dir,
    intake_dir,
    worktrees_dir,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BACKLOG_DIR = backlog_dir()
INTAKE_DIR = intake_dir()
WORKTREES_DIR = worktrees_dir()

_SLUG_RE = re.compile(r'[^a-z0-9]+')

def slugify(title: str) -> str:
    """Lowercase, replace non-alnum with '-', strip leading/trailing '-'. Returns 'item' if empty."""
    s = _SLUG_RE.sub('-', (title or '').lower()).strip('-')
    return s or 'item'

def _git(args: list, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    """Run git with list-form args. Never uses shell. Returns CompletedProcess."""
    cmd = ['git'] + list(args)
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)

def list_runner_branches() -> list:
    """Return list of local branch names starting with 'runner/'. Empty on error."""
    r = _git(['for-each-ref', '--format=%(refname:short)', 'refs/heads/runner/'])
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

def list_unmerged_runner_branches(base: str = 'master') -> list:
    """Return runner/* branches not merged into base."""
    r = _git(['branch', '--no-merged', base, '--list', 'runner/*'])
    if r.returncode != 0:
        return []
    out = []
    for ln in r.stdout.splitlines():
        name = ln.strip().lstrip('*').strip()
        if name.startswith('runner/'):
            out.append(name)
    return out

def hygiene_precheck(max_unreviewed: int, base: str = 'master') -> Optional[str]:
    """Return None if ok to proceed, else a skip reason string like 'queue full (5/5)'."""
    branches = list_unmerged_runner_branches(base)
    if len(branches) >= max_unreviewed:
        return f'queue full ({len(branches)}/{max_unreviewed})'
    return None

def _branch_exists_for_uuid(uuid: str) -> bool:
    """True if any runner/* branch name contains the uuid."""
    for b in list_runner_branches():
        if uuid in b:
            return True
    return False

def pick_backlog_item() -> Optional[Path]:
    """Return oldest-mtime runner/backlog/*.json whose uuid has no existing runner/* branch. None if none."""
    if not BACKLOG_DIR.exists():
        return None
    candidates = sorted(BACKLOG_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime)
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        uid = data.get('id')
        if not isinstance(uid, str) or not uid.strip():
            continue
        if _branch_exists_for_uuid(uid.strip()):
            continue
        return p
    return None

def all_backlog_items_claimed() -> bool:
    """True if backlog dir is non-empty but all items already have open branches."""
    if not BACKLOG_DIR.exists():
        return False
    files = list(BACKLOG_DIR.glob('*.json'))
    if not files:
        return False
    for p in files:
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        uid = data.get('id')
        if isinstance(uid, str) and uid.strip() and not _branch_exists_for_uuid(uid.strip()):
            return False
    return True

def worktrees_dir_for(target_repo: Path | None = None) -> Path:
    """Return the .runner-worktrees/ path for a given target repo.

    Defaults to apiary's WORKTREES_DIR for backwards compatibility. Phase 3
    of the multi-repo rearchitecture will start passing non-apiary targets;
    until then every caller resolves to the same apiary path it always did.
    """
    if target_repo is None:
        return WORKTREES_DIR
    return Path(target_repo) / '.runner-worktrees'


def git_worktree_create(
    branch: str,
    base: str = 'master',
    *,
    target_repo: Path | None = None,
) -> tuple:
    """Create an isolated git worktree on a new branch from base.

    Returns (ok, worktree_path_or_None, stderr). The worktree lives at
    ``<target_repo>/.runner-worktrees/<safe-branch>/`` and is checked out
    from ``base``; the branch is created fresh. Caller is responsible for
    git_worktree_remove.

    ``target_repo`` (phase 1 of the multi-repo rearchitecture) controls
    which repo owns the worktree. Defaults to apiary REPO_ROOT, so
    pre-existing callers see no behavior change. When set, both the
    ``.runner-worktrees/`` directory and the ``git worktree add`` command
    operate on the target repo's git tree.

    Detached mode uses this so a runner pass cannot disturb the operator's
    main checkout.
    """
    repo = Path(target_repo) if target_repo is not None else REPO_ROOT
    worktrees_dir = worktrees_dir_for(target_repo)
    try:
        worktrees_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, None, f'could not create worktrees dir: {e}'
    safe = branch.replace('/', '_').replace('\\', '_')
    wt_path = worktrees_dir / safe
    if wt_path.exists():
        return False, None, f'worktree path already exists: {wt_path}'
    r = _git(['worktree', 'add', '-b', branch, str(wt_path), base], cwd=repo)
    if r.returncode != 0:
        return False, None, r.stderr
    return True, wt_path, ''

def git_commit_all_in(cwd: Path, message: str) -> tuple:
    """git add -A and git commit -m message inside `cwd` (a worktree). Allows empty."""
    r = _git(['add', '-A'], cwd=cwd)
    if r.returncode != 0:
        return False, r.stderr
    r = _git(['commit', '-m', message, '--allow-empty'], cwd=cwd)
    if r.returncode != 0:
        return False, r.stderr
    return True, ''

def git_worktree_remove(path: Path) -> tuple:
    """git worktree remove --force <path>. Idempotent — returns ok if path is gone."""
    if not path.exists():
        return True, ''
    r = _git(['worktree', 'remove', '--force', str(path)])
    return (r.returncode == 0, r.stderr)

def _list_detached_worktrees() -> list:
    """Parse `git worktree list --porcelain` and return [(path, branch), ...]
    for worktrees whose path is under WORKTREES_DIR. Branch is the short
    ref name (no refs/heads/) or '' if detached."""
    r = _git(['worktree', 'list', '--porcelain'])
    if r.returncode != 0:
        return []
    out = []
    cur_path = None
    cur_branch = ''
    try:
        wt_root = WORKTREES_DIR.resolve()
    except OSError:
        return []
    wt_root_str = str(wt_root)
    for line in r.stdout.splitlines() + ['']:
        if line.startswith('worktree '):
            cur_path = line[len('worktree '):].strip()
        elif line.startswith('branch '):
            br = line[len('branch '):].strip()
            if br.startswith('refs/heads/'):
                br = br[len('refs/heads/'):]
            cur_branch = br
        elif line == '':
            if cur_path:
                try:
                    p = Path(cur_path).resolve()
                    if str(p).startswith(wt_root_str):
                        out.append((p, cur_branch))
                except OSError:
                    pass
            cur_path = None
            cur_branch = ''
    return out

def _branch_has_commits_beyond(branch: str, base: str = 'master') -> bool:
    """True if `branch` has at least one commit missing from `base`."""
    if not branch:
        return False
    r = _git(['rev-list', '--count', f'{base}..{branch}'])
    if r.returncode != 0:
        return False
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False

def prune_stale_worktrees() -> list:
    """Remove detached-runner worktrees orphaned by a previous hard kill
    (e.g. `Stop-ScheduledTask`, `taskkill /F`). Worktrees whose branch has
    commits beyond master are preserved — they may contain partial work
    awaiting review. Returns a list of (path, action) tuples for logging.

    Called at the start of every detached run so a single hard-kill does
    not permanently block the scheduled retry path.
    """
    results = []
    for wt, branch in _list_detached_worktrees():
        if _branch_has_commits_beyond(branch):
            results.append((wt, 'preserved'))
            continue
        r = _git(['worktree', 'remove', '--force', str(wt)])
        if r.returncode == 0:
            if branch:
                _git(['branch', '-D', branch])
            results.append((wt, 'removed'))
        else:
            results.append((wt, 'failed'))
    # Clean up directories git no longer tracks (registration was pruned but
    # the files on disk survived, or the dir was never registered at all).
    if WORKTREES_DIR.exists():
        known = {p for p, _ in _list_detached_worktrees()}
        for entry in WORKTREES_DIR.iterdir():
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if entry.is_dir() and resolved not in known:
                shutil.rmtree(entry, ignore_errors=True)
                results.append((resolved, 'rmtree'))
    _git(['worktree', 'prune'])
    return results
