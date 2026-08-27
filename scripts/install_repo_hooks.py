#!/usr/bin/env python3
"""Install main-apiary's own .git/hooks/{pre-commit, commit-msg, post-merge} scripts.

These are repo-local git hooks that run on commits/merges in the apiary
checkout itself — not Claude Code hooks, and unrelated to the per-repo
install model. They live in main-apiary's ``.git/hooks/`` and reference
sources in ``docs/hooks/`` and ``runner/hooks/``.

Split out of the old unified installer during the per-repo migration;
the Claude Code side of the install now lives in ``core/install.py``
(see ``docs/architecture/per-repo-install.md``).

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

from scripts.install_git_hooks import hooks_dir  # noqa: E402


def _git_hooks_dir() -> Path:
    """Where git actually looks for this repo's hooks.

    Honours ``core.hooksPath``: installing into ``.git/hooks`` while that is
    set writes a hook git never runs, and the installer would report success
    over a dead gate.
    """
    target, warning = hooks_dir(REPO_ROOT)
    if warning:
        print(f"  WARNING: {warning}")
    return target


def install_pre_commit_hook() -> None:
    """Install ``docs/hooks/pre-commit`` into ``.git/hooks/pre-commit``.

    The hook chains the commit-time gates: ``docs/check.py`` (framework doc
    conformance), ``docs/check_cli_claims.py`` (cli-tools.md vs each tool's
    real argparse), the generated-table checks (``docs/generate_cli_docs.py``,
    ``docs/generate_reference.py``), ``docs/change_map.py --staged`` (mapped
    code needs its doc), ``scripts/secret_scan.py --staged`` and ``ruff check``
    on the staged .py files. Any one failing blocks. Skipped silently if ``.git/hooks/``
    doesn't exist (e.g. on a sparse checkout).

    Conflict policy: a pre-commit hook that doesn't reference ``docs/check.py``
    is treated as somebody else's and left alone. An older apiary hook (doc
    check only) still matches, so re-running upgrades it in place to the
    combined version.

    Side projects get the secret scan on its own via
    ``scripts/install_git_hooks.py`` — they have no framework docs to check.
    """
    git_hooks_dir = _git_hooks_dir()
    if not git_hooks_dir.is_dir():
        print(f"  Pre-commit hook  : skipped ({git_hooks_dir} not found)")
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


def install_commit_msg_hook() -> None:
    """Install ``docs/hooks/commit-msg`` into ``.git/hooks/commit-msg``.

    It reads the ``docs: unchanged`` trailer that waives the change-map check
    (git has not written the message yet when pre-commit runs). Same conflict
    policy as pre-commit: a commit-msg hook that does not mention
    ``change_map`` is somebody else's and is left alone.
    """
    git_hooks_dir = _git_hooks_dir()
    if not git_hooks_dir.is_dir():
        print(f"  Commit-msg hook  : skipped ({git_hooks_dir} not found)")
        return
    source = DOCS_DIR / "hooks" / "commit-msg"
    if not source.is_file():
        print(f"  Commit-msg hook  : skipped ({source} not found)")
        return
    target = git_hooks_dir / "commit-msg"
    if target.exists():
        content = target.read_text(encoding="utf-8")
        if "change_map" not in content and "docs: unchanged" not in content:
            print(f"  Commit-msg hook  : WARNING — {target} already exists (not ours), skipping")
            return
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | 0o755)  # no-op on Windows
    print(f"  Commit-msg hook  : {target}")


def install_post_merge_hook() -> None:
    """Install ``runner/hooks/post-merge`` into ``.git/hooks/post-merge``.

    The hook closes the scribe TODO linked to a merged runner branch.
    Skipped when source is missing (older clones). Same conflict policy
    as pre-commit: leave non-apiary hooks alone.
    """
    git_hooks_dir = _git_hooks_dir()
    if not git_hooks_dir.is_dir():
        print(f"  Post-merge hook  : skipped ({git_hooks_dir} not found)")
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
    install_commit_msg_hook()
    install_post_merge_hook()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
