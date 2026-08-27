#!/usr/bin/env python3
"""Helpers for the detached cron-driven runner mode.

Every git call in here takes an explicit repo to run in. It used to default
to apiary's own checkout (``_git(args, cwd=REPO_ROOT)``), which meant a run
against ``--target-repo X`` still listed *apiary's* branches, pruned
*apiary's* worktrees and tried to remove the run's worktree from *apiary*
(review runner Bug 2/Bug 4). ``_git`` now requires ``cwd``, so a caller that
forgets the target fails loudly at import/call time instead of silently
inspecting the wrong repository.
"""
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


def slugify(title: str, *, max_length: int | None = None,
            fallback: str = 'item') -> str:
    """Lowercase, replace runs of non-alnum with '-', strip leading/trailing '-'.

    The one slugifier in the package: the run branch uses it uncapped with an
    'item' fallback, and ticket filenames use it capped at 60 with an empty
    fallback so a title that slugs to nothing is reported rather than silently
    written as `item.json`. There were three near-identical copies (review X-3).
    """
    s = _SLUG_RE.sub('-', (title or '').lower()).strip('-')
    if max_length is not None:
        s = s[:max_length].strip('-')
    return s or fallback


def _git(args: list, *, cwd: Path) -> subprocess.CompletedProcess:
    """Run git with list-form args inside *cwd*. Never uses shell.

    ``cwd`` is mandatory and keyword-only: which repository a git command
    runs against is the whole question in multi-repo mode, and a default
    made every caller silently correct-looking and wrong.
    """
    cmd = ['git'] + list(args)
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding='utf-8',
    )


def list_runner_branches(repo: Path) -> list:
    """Return local branch names starting with 'runner/' in *repo*. Empty on error."""
    r = _git(['for-each-ref', '--format=%(refname:short)', 'refs/heads/runner/'], cwd=repo)
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def list_unmerged_runner_branches(repo: Path, base: str = 'master') -> list:
    """Return *repo*'s runner/* branches not merged into base."""
    r = _git(['branch', '--no-merged', base, '--list', 'runner/*'], cwd=repo)
    if r.returncode != 0:
        return []
    out = []
    for ln in r.stdout.splitlines():
        name = ln.strip().lstrip('*').strip()
        if name.startswith('runner/'):
            out.append(name)
    return out


def hygiene_precheck(max_unreviewed: int, repo: Path, base: str = 'master') -> Optional[str]:
    """Return None if ok to proceed, else a skip reason like 'queue full (5/5)'."""
    branches = list_unmerged_runner_branches(repo, base)
    if len(branches) >= max_unreviewed:
        return f'queue full ({len(branches)}/{max_unreviewed})'
    return None


def _branch_exists_for_uuid(uuid: str, repo: Path) -> bool:
    """True if any runner/* branch name in *repo* contains the uuid."""
    for b in list_runner_branches(repo):
        if uuid in b:
            return True
    return False


def _repo_for_item(data: dict, default_repo: Path) -> Path:
    """Which repo a backlog item's in-flight branch would live in.

    A backlog item may name its own ``target_repo``; the claimed-branch check
    has to look there, not in whichever repo this invocation defaults to.
    """
    field = data.get('target_repo')
    if isinstance(field, str) and field.strip():
        return Path(field.strip())
    return default_repo


def pick_backlog_item(repo: Path) -> Optional[Path]:
    """Oldest-mtime backlog/*.json whose uuid has no runner/* branch yet.

    *repo* is the default target; an item carrying its own ``target_repo``
    is checked against that repo instead.
    """
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
        if _branch_exists_for_uuid(uid.strip(), _repo_for_item(data, repo)):
            continue
        return p
    return None


def all_backlog_items_claimed(repo: Path) -> bool:
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
        if (
            isinstance(uid, str) and uid.strip()
            and not _branch_exists_for_uuid(uid.strip(), _repo_for_item(data, repo))
        ):
            return False
    return True


def worktrees_dir_for(target_repo: Path | None = None) -> Path:
    """Return the live-worktrees directory for a given target repo.

    Single definition, shared with ``target_repo.worktrees_dir`` — the two
    used to disagree (``.runner-worktrees`` vs ``.apiary/runner-worktrees``),
    so ``prune_stale_worktrees`` scanned a directory nothing ever wrote to
    (review runner Bug 4).
    """
    if target_repo is None:
        return WORKTREES_DIR
    return worktrees_dir(Path(target_repo))


def git_worktree_create(
    branch: str,
    base: str = 'master',
    *,
    target_repo: Path,
) -> tuple:
    """Create an isolated git worktree on a new branch from base.

    Returns (ok, worktree_path_or_None, stderr). The worktree lives at
    ``<target_repo>/.runner-worktrees/<safe-branch>/`` and is checked out
    from ``base``; the branch is created fresh. Caller is responsible for
    git_worktree_remove.

    ``target_repo`` controls which repo owns the worktree: both the
    ``.runner-worktrees/`` directory and the ``git worktree add`` command
    operate on that repo's git tree.

    Detached mode uses this so a runner pass cannot disturb the operator's
    main checkout.
    """
    repo = Path(target_repo)
    wt_dir = worktrees_dir_for(repo)
    try:
        wt_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, None, f'could not create worktrees dir: {e}'
    safe = branch.replace('/', '_').replace('\\', '_')
    wt_path = wt_dir / safe
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


def git_worktree_remove(path: Path, *, target_repo: Path) -> tuple:
    """git worktree remove --force <path>. Idempotent — returns ok if path is gone."""
    if not path.exists():
        return True, ''
    r = _git(['worktree', 'remove', '--force', str(path)], cwd=Path(target_repo))
    return (r.returncode == 0, r.stderr)


def _list_detached_worktrees(repo: Path) -> list:
    """Parse `git worktree list --porcelain` in *repo* and return
    [(path, branch), ...] for worktrees under that repo's worktrees dir.
    Branch is the short ref name (no refs/heads/) or '' if detached."""
    r = _git(['worktree', 'list', '--porcelain'], cwd=repo)
    if r.returncode != 0:
        return []
    out = []
    cur_path = None
    cur_branch = ''
    try:
        wt_root = worktrees_dir_for(repo).resolve()
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


def _branch_has_commits_beyond(branch: str, repo: Path, base: str = 'master') -> bool:
    """True if `branch` has at least one commit missing from `base` in *repo*."""
    if not branch:
        return False
    r = _git(['rev-list', '--count', f'{base}..{branch}'], cwd=repo)
    if r.returncode != 0:
        return False
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False


def prune_stale_worktrees(target_repo: Path) -> list:
    """Remove detached-runner worktrees orphaned by a previous hard kill
    (e.g. `Stop-ScheduledTask`, `taskkill /F`). Worktrees whose branch has
    commits beyond master are preserved — they may contain partial work
    awaiting review. Returns a list of (path, action) tuples for logging.

    Called at the start of every detached run so a single hard-kill does
    not permanently block the scheduled retry path.
    """
    repo = Path(target_repo)
    wt_root = worktrees_dir_for(repo)
    results = []
    for wt, branch in _list_detached_worktrees(repo):
        if _branch_has_commits_beyond(branch, repo):
            results.append((wt, 'preserved'))
            continue
        r = _git(['worktree', 'remove', '--force', str(wt)], cwd=repo)
        if r.returncode == 0:
            if branch:
                _git(['branch', '-D', branch], cwd=repo)
            results.append((wt, 'removed'))
        else:
            results.append((wt, 'failed'))
    # Clean up directories git no longer tracks (registration was pruned but
    # the files on disk survived, or the dir was never registered at all).
    if wt_root.exists():
        known = {p for p, _ in _list_detached_worktrees(repo)}
        for entry in wt_root.iterdir():
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if entry.is_dir() and resolved not in known:
                shutil.rmtree(entry, ignore_errors=True)
                results.append((resolved, 'rmtree'))
    _git(['worktree', 'prune'], cwd=repo)
    return results
