"""Git helpers shared by every apiary subsystem.

One resolver for "which repo am I in". Before this module the same
``git rev-parse --show-toplevel`` subprocess block was copy-pasted eight
times (core/utils/state.py, core/flags.py, core/git_hooks.py,
core/hooks/pre_push_doc_conformer.py, scribe/notes.py, and the three
``*/store.py`` modules) — review finding X-3. Import ``git_root`` instead
of writing a ninth.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# git is a subprocess, and hooks run on the tool-call hot path. A repo on a
# cold network share can stall; five seconds is long enough for any local
# checkout and short enough that a wedged git never wedges a hook.
_GIT_TIMEOUT_SECONDS = 5


def git_root(start: Path | str | None = None) -> Path | None:
    """Return the git work-tree root containing *start* (default: cwd).

    Returns ``None`` — never raises — when git is unavailable, when
    *start* is not inside a work tree, when the path does not exist, or
    when git takes longer than ``_GIT_TIMEOUT_SECONDS``. Callers decide
    what "not in a repo" means for them; this helper has no fallback of
    its own.

    In a linked worktree this returns the *worktree's* root, which is what
    every caller wants (it is the checkout being worked on). Use
    :func:`main_worktree_root` when you need the shared main checkout.
    """
    cwd = str(start) if start is not None else str(Path.cwd())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # OSError covers FileNotFoundError (no git) and a missing cwd;
        # ValueError covers a null byte in the path; SubprocessError
        # covers TimeoutExpired.
        return None
    if result.returncode != 0:
        return None
    top = (result.stdout or "").strip()
    return Path(top) if top else None


def main_worktree_root(start: Path | str | None = None) -> Path | None:
    """Return the *main* checkout's root for *start*, de-worktreeing it.

    ``git rev-parse --git-common-dir`` names the shared ``.git`` directory:
    ``<main>/.git`` for the main checkout and for a linked worktree alike.
    Its parent is therefore the main working tree in both cases, so a
    caller that must not treat a throwaway worktree as a separate repo
    (``resolve_apiary_repo`` — review Phase 3.2: worktrees created a second
    ``.repos/`` registry) can collapse the two.

    Returns ``None`` under the same conditions as :func:`git_root`, and
    also for a bare repo (no working tree to name).
    """
    cwd = str(start) if start is not None else str(Path.cwd())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # --path-format landed in git 2.31; on anything older fall back to
        # the plain worktree root rather than reporting "not a repo".
        return git_root(start)
    common = (result.stdout or "").strip()
    if not common:
        return git_root(start)
    root = Path(common).parent
    return root if root.is_dir() else None
