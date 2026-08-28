#!/usr/bin/env python3
"""Install the secret-scan pre-commit hook into the CURRENT repo.

Thin CLI over :mod:`core.git_hooks`, which holds the logic so ``core.install``
can install the hook on every bootstrap. Bootstrapping is the reliable moment:
a one-time sweep decays as soon as a new repo is registered (#T-2026-261).

Sibling of ``scripts/install_repo_hooks.py``, which targets main-apiary's own
checkout and installs the combined doc-check + secret-scan hook. This one
targets whatever repo you run it from, so an apiary-managed side project gets
the same commit-time protection.

Run it through the per-repo launcher so it works from any managed repo::

    python .claude/apiary/launch.py scripts/install_git_hooks.py
    python .claude/apiary/launch.py scripts/install_git_hooks.py --uninstall
    python .claude/apiary/launch.py scripts/install_git_hooks.py --list

Since ``apiary install`` now does this, reach for it by hand only to retrofit a
repo that predates the change, or to undo/inspect an install.

Exit codes::
    0  success (or nothing to do)
    1  refused — a foreign pre-commit hook is in the way, or not a git repo
    2  bad arguments
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.git_hooks import (  # noqa: E402
    HOOK_SOURCE,
    OWNED_MARKER,
    _classify,
    classify,
    configured_hooks_path,
    hook_path,
    hooks_dir,
    install,
    report,
    uninstall,
)
from core.utils.gitutil import git_root  # noqa: E402

__all__ = [
    "HOOK_SOURCE",
    "OWNED_MARKER",
    "classify",
    "_classify",
    "configured_hooks_path",
    "git_root",
    "hook_path",
    "hooks_dir",
    "install",
    "report",
    "uninstall",
    "main",
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--uninstall", action="store_true", help="Remove the hook if we own it.")
    group.add_argument("--list", dest="list_mode", action="store_true", help="Report status only.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing non-apiary pre-commit hook.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Target repo (default: the git repo containing the working directory).",
    )
    args = parser.parse_args(argv)

    start = (args.repo or Path.cwd()).expanduser()
    repo = git_root(start)
    if repo is None:
        print(f"error: {start} is not inside a git repository", file=sys.stderr)
        return 1

    print(f"Target repo: {repo}")
    if args.list_mode:
        return report(repo)
    if args.uninstall:
        return uninstall(repo)
    return install(repo, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
