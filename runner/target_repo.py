"""Resolve which repo a runner pass operates on (multi-repo support, phase 1).

The runner historically operated only on the apiary checkout it ships with.
Phase 1 of the multi-repo rearchitecture introduces the *concept* of a
configurable target repo without changing default behavior. Subsequent
phases plumb the resolver into git_worktree_create (phase 1, here),
the stage subprocess loader (phase 2), the CLI / intake schema
(phase 3), and the auto_plan prompt (phase 4).

Resolution precedence (highest first):
  1. Explicit ``cli_override`` argument (set by phase 3's --target-repo flag).
  2. Intake dict's ``target_repo`` field, if present and non-empty (phase 3).
  3. Config default ``runner.target_repo`` from runner/config.json (phase 3).
  4. Fallback: apiary REPO_ROOT — preserves all current behavior when nothing
     else is configured.

Phase 1 wires only the fallback path, so existing callers see no behavior
change. Phase 3 lights up the higher-priority sources.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional, Union

from .config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
APIARY_REPO_ROOT = SCRIPT_DIR.parent

PathLike = Union[str, Path]


def choose_target_repo(
    *,
    cli_override: Optional[PathLike] = None,
    intake: Optional[dict] = None,
    apiary_root: Optional[Path] = None,
) -> Path:
    """Pure picker — apply the precedence rules and return a Path.

    Does NOT validate that the path exists or is a git repo. Use
    ``resolve_target_repo`` for the validating wrapper. Split out as a
    pure function so precedence rules can be unit-tested without any
    fixture repos.
    """
    if cli_override:
        return Path(cli_override)
    if intake is not None:
        field = intake.get("target_repo")
        if isinstance(field, str) and field.strip():
            return Path(field.strip())
    cfg_default = cfg("runner", "target_repo", None)
    if isinstance(cfg_default, str) and cfg_default.strip():
        return Path(cfg_default.strip())
    return apiary_root if apiary_root is not None else APIARY_REPO_ROOT


def resolve_target_repo(
    *,
    cli_override: Optional[PathLike] = None,
    intake: Optional[dict] = None,
    apiary_root: Optional[Path] = None,
) -> Path:
    """Resolve and validate the target repo path.

    Same precedence as ``choose_target_repo``, then verifies the chosen
    path exists, is a directory, and contains a ``.git`` entry. Note
    ``.git`` may be either a directory (normal repo) or a file (git
    worktree, submodule) — both are valid. Raises ``ValueError`` if any
    check fails. Returns the resolved (absolute) path.
    """
    chosen = choose_target_repo(
        cli_override=cli_override, intake=intake, apiary_root=apiary_root,
    )
    chosen = chosen.resolve()
    if not chosen.exists():
        raise ValueError(f"target_repo path does not exist: {chosen}")
    if not chosen.is_dir():
        raise ValueError(f"target_repo path is not a directory: {chosen}")
    git_marker = chosen / ".git"
    if not git_marker.exists():
        raise ValueError(
            f"target_repo path is not a git repository (no .git entry): {chosen}"
        )
    return chosen
