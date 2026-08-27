"""Resolve which repo a runner pass operates on (multi-repo support).

Resolution precedence (highest first):
  1. Explicit ``cli_override`` argument (set by --target-repo flags).
  2. Intake dict's ``target_repo`` field, if present and non-empty.
  3. ``APIARY_TARGET_REPO`` environment variable — set by run.py after it
     resolves the target so spawned subprocesses inherit the choice.
  4. Config default ``runner.target_repo`` from runner/config.json.
  5. Fallback: apiary repo root — preserves all single-repo behavior when
     nothing else is configured.

The path helpers (intake_dir, plans_dir, etc.) check the env var on each
call so subprocess chains (run.py → auto_refine → ...) stay target-aware
without every call site passing the target explicitly.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional, Union

from .config_loader import get as cfg

SCRIPT_DIR = Path(__file__).resolve().parent
APIARY_REPO_ROOT = SCRIPT_DIR.parent

ACTIVE_TARGET_ENV = "APIARY_TARGET_REPO"

PathLike = Union[str, Path]


def set_active_target(target: PathLike) -> None:
    """Publish the resolved target into the environment so subprocesses and
    helper calls in the same process inherit it. Called by the orchestrator
    after ``resolve_target_repo`` so every downstream stage / path helper
    sees the same value without having to plumb an argument through."""
    os.environ[ACTIVE_TARGET_ENV] = str(Path(target).resolve())


def clear_active_target() -> None:
    """Remove the env var. Tests use this to isolate between cases."""
    os.environ.pop(ACTIVE_TARGET_ENV, None)


def _env_target() -> Optional[Path]:
    value = os.environ.get(ACTIVE_TARGET_ENV, "").strip()
    return Path(value) if value else None


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
    env = _env_target()
    if env is not None:
        return env
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


# -----------------------------------------------------------------------------
# Artifact path helpers — return per-target-repo paths under .apiary/runner/.
# Callers resolve the target once (via resolve_target_repo / choose_target_repo
# or a direct Path) and pass it to these helpers. Passing None falls back to
# the apiary repo root so legacy call sites continue working unchanged until
# they're plumbed with a resolved target.

_RUNNER_STATE_DIR = ".apiary/runner"
_RUNNER_SUBDIR = "runner"
_TARGET_STATE_DIR_ENV = "APIARY_TARGET_STATE_DIR"


def _default_target(target: Optional[Path]) -> Path:
    if target is not None:
        return Path(target).resolve()
    env = _env_target()
    if env is not None:
        return env.resolve()
    return APIARY_REPO_ROOT


def artifacts_root(target: Optional[Path] = None) -> Path:
    """Return the umbrella directory for runner state.

    Resolution order:
      1. ``APIARY_TARGET_STATE_DIR`` env var (set by apiary_launch.py after
         the registry resolver runs) — returns ``<state_dir>/runner/``.
         Used when the caller did not pass an explicit *target* override.
      2. ``<target>/.apiary/runner/`` — legacy in-repo path. Used when an
         explicit *target* is passed (e.g. multi-repo runner orchestrating
         a different repo than the one apiary is registered against), or
         as a fallback when the env var is unset.
    """
    if target is None:
        env_state = os.environ.get(_TARGET_STATE_DIR_ENV, "").strip()
        if env_state:
            return Path(env_state) / _RUNNER_SUBDIR
    return _default_target(target) / _RUNNER_STATE_DIR


def intake_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "intake"


def backlog_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "backlog"


def specs_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "specs"


def plans_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "plans"


def executions_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "executions"


def hardens_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "hardens"


def reports_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "reports"


def locks_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "locks"


def runs_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "runs"


def logs_dir(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "logs"


def run_history_path(target: Optional[Path] = None) -> Path:
    return artifacts_root(target) / "run_history.jsonl"



def worktrees_dir(target: Optional[Path] = None) -> Path:
    """Live git worktrees under the target repo.

    Note the path is ``<target>/.apiary/runner-worktrees/`` (sibling to
    ``.apiary/runner/``) so live checkouts don't collide with state
    artifacts.
    """
    return _default_target(target) / ".apiary" / "runner-worktrees"
