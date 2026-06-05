#!/usr/bin/env python3
"""Install main-apiary's own .git/hooks/{pre-commit, post-merge} scripts.

These are repo-local git hooks that run on commits/merges in the apiary
checkout itself — not Claude Code hooks, and unrelated to the per-repo
install model. They live in main-apiary's ``.git/hooks/`` and reference
sources in ``docs/hooks/`` and ``runner/hooks/``.

Pulled out of ``setup.py`` during the per-repo migration so setup.py
could be reduced to a thin redirect stub (``--global`` is gone — see
``MIGRATION-PLAN.md`` §10 phase 5).

Usage::

    python scripts/install_repo_hooks.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DOCS_DIR = REPO_ROOT / "docs"
RUNNER_DIR = REPO_ROOT / "runner"


def install_pre_commit_hook() -> None:
    """Install ``docs/hooks/pre-commit`` into ``.git/hooks/pre-commit``.

    The hook runs ``docs/check.py`` before commits. Skipped silently if
    ``.git/hooks/`` doesn't exist (e.g. on a sparse checkout). If a non-
    apiary pre-commit hook is already present, the installer leaves it
    alone — the operator should investigate before overwriting.
    """
    git_hooks_dir = REPO_ROOT / ".git" / "hooks"
    if not git_hooks_dir.is_dir():
        print(f"  Pre-commit hook  : skipped (.git/hooks/ not found)")
        return

    target = git_hooks_dir / "pre-commit"
    source = DOCS_DIR / "hooks" / "pre-commit"

    if target.exists():
        content = target.read_text(encoding="utf-8")
        if "docs/check.py" not in content:
            print(f"  Pre-commit hook  : WARNING — {target} already exists (not ours), skipping")
            return

    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | 0o755)  # no-op on Windows
    print(f"  Pre-commit hook  : {target}")


def install_post_merge_hook() -> None:
    """Install ``runner/hooks/post-merge`` into ``.git/hooks/post-merge``.

    The hook closes the scribe TODO linked to a merged runner branch.
    Skipped when source is missing (older clones). Same conflict policy
    as pre-commit: leave non-apiary hooks alone.
    """
    git_hooks_dir = REPO_ROOT / ".git" / "hooks"
    if not git_hooks_dir.is_dir():
        print(f"  Post-merge hook  : skipped (.git/hooks/ not found)")
        return

    target = git_hooks_dir / "post-merge"
    source = RUNNER_DIR / "hooks" / "post-merge"

    if not source.exists():
        print(f"  Post-merge hook  : skipped (source not found: {source})")
        return

    if target.exists():
        content = target.read_text(encoding="utf-8")
        if "runner.close_source_todo" not in content:
            print(f"  Post-merge hook  : WARNING — {target} already exists (not ours), skipping")
            return

    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | 0o755)
    print(f"  Post-merge hook  : {target}")


def main() -> int:
    print(f"Installing repo-local git hooks into {REPO_ROOT / '.git' / 'hooks'}")
    install_pre_commit_hook()
    install_post_merge_hook()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
