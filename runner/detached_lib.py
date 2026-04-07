#!/usr/bin/env python3
"""Helpers for the detached cron-driven runner mode."""
from __future__ import annotations
import json, os, re, subprocess, sys, uuid as uuid_mod
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BACKLOG_DIR = SCRIPT_DIR / 'backlog'
INTAKE_DIR = SCRIPT_DIR / 'intake'
OVERNIGHT_LOG = SCRIPT_DIR / 'overnight.jsonl'

_SLUG_RE = re.compile(r'[^a-z0-9]+')

def slugify(title: str) -> str:
    """Lowercase, replace non-alnum with '-', strip leading/trailing '-'. Returns 'item' if empty."""
    s = _SLUG_RE.sub('-', (title or '').lower()).strip('-')
    return s or 'item'

def short_uuid() -> str:
    """Return first 8 chars of a uuid4 hex."""
    return uuid_mod.uuid4().hex[:8]

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

def append_overnight_log(entry: dict) -> bool:
    """Append one JSON line to overnight.jsonl. Returns True on success, False on OSError (prints warning to stderr)."""
    try:
        OVERNIGHT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with OVERNIGHT_LOG.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
        return True
    except OSError as e:
        print(f'WARN: overnight log write failed: {e}', file=sys.stderr)
        return False

def git_create_branch(branch: str, base: str = 'master') -> tuple:
    """Create and checkout branch from base. Returns (ok: bool, stderr: str)."""
    # Check working tree is clean
    r = _git(['status', '--porcelain'])
    if r.returncode != 0:
        return False, r.stderr
    if r.stdout.strip():
        return False, 'working tree has uncommitted changes'
    r = _git(['checkout', base])
    if r.returncode != 0:
        return False, r.stderr
    r = _git(['checkout', '-b', branch])
    if r.returncode != 0:
        return False, r.stderr
    return True, ''

def git_commit_all(message: str) -> tuple:
    """git add -A and git commit -m message. Returns (ok, stderr). Allows empty if nothing changed."""
    r = _git(['add', '-A'])
    if r.returncode != 0:
        return False, r.stderr
    r = _git(['commit', '-m', message, '--allow-empty'])
    if r.returncode != 0:
        return False, r.stderr
    return True, ''

def git_checkout(ref: str) -> tuple:
    r = _git(['checkout', ref])
    return (r.returncode == 0, r.stderr)
