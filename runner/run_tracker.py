"""Cross-invocation run tracking for detached runner mode.

Each intake UUID gets a tracker file at ``runner/runs/<uuid>.json`` that
persists across cron invocations.  The tracker records cumulative token
spend and attempt count so the orchestrator can enforce cross-run caps
and detect which stage to resume from.
"""

import datetime
import json
from pathlib import Path

from .target_repo import (
    executions_dir,
    hardens_dir,
    plans_dir,
    runs_dir,
    specs_dir,
)

SCRIPT_DIR = Path(__file__).resolve().parent
RUNS_DIR = runs_dir()


def _path(uuid: str) -> Path:
    return RUNS_DIR / f"{uuid}.json"


def load(uuid: str) -> dict:
    """Read the tracker for *uuid*. Returns ``{}`` if no tracker exists."""
    p = _path(uuid)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save(uuid: str, data: dict) -> None:
    """Write *data* as the tracker for *uuid*."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _path(uuid).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def record_attempt(
    uuid: str,
    tokens_this_run: int,
    stages_completed: int,
    exit_status: str,
    last_stage_completed: str | None = None,
) -> dict:
    """Append an attempt to the tracker and return the updated tracker dict."""
    tracker = load(uuid)
    tracker.setdefault("uuid", uuid)
    tracker.setdefault("attempts", [])

    prev_total = tracker.get("total_tokens", 0)
    tracker["total_tokens"] = prev_total + tokens_this_run
    tracker["attempt_count"] = len(tracker["attempts"]) + 1
    tracker["last_exit_status"] = exit_status
    if last_stage_completed is not None:
        tracker["last_stage_completed"] = last_stage_completed

    tracker["attempts"].append(
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tokens": tokens_this_run,
            "exit_status": exit_status,
            "stages_completed": stages_completed,
        }
    )

    save(uuid, tracker)
    return tracker


# Maps each artifact key to the dir-returning helper, the stage to resume
# from, and a predicate deciding whether the artifact represents COMPLETED
# work. Latest-to-earliest order so we resume as late as possible.
#
# The predicates exist because failed stages also write artifacts:
# auto_plan saves its best attempt with valid=false for diagnosis, and the
# executor persists an aborted execution log. Resuming *past* those (retry
# of 2026-08-31: plan valid=false -> "resuming from executor" -> executor
# refused with "Plan is not valid") turns every failed stage into a wall
# on the next attempt.


def _read_artifact(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _plan_is_valid(path: Path) -> bool:
    data = _read_artifact(path)
    return data is not None and data.get("valid") is True


def _execution_completed(path: Path) -> bool:
    data = _read_artifact(path)
    return data is not None and data.get("status") == "completed"


def _always(_path: Path) -> bool:
    return True


_ARTIFACT_RESUME_MAP = [
    (hardens_dir, "approval", _always),
    (executions_dir, "auto_harden", _execution_completed),
    (plans_dir, "executor", _plan_is_valid),
    (specs_dir, "auto_plan", _always),
]


def get_resume_stage(uuid: str, worktree_path: Path | None = None) -> str | None:
    """Determine which stage to resume from based on produced artifacts.

    Review artifacts (specs/plans/executions/hardens/reports) live under
    ``<apiary>/.apiary/runner/`` today. ``worktree_path`` is retained for
    backward compatibility but ignored.

    An artifact only counts as a resume point when its predicate says the
    stage actually completed -- an invalid plan or an aborted execution is
    a failure record, and the lookup falls through to the stage that must
    be re-run.
    """
    for dir_fn, resume_stage, is_complete in _ARTIFACT_RESUME_MAP:
        artifact = dir_fn() / f"{uuid}.json"
        if artifact.exists() and is_complete(artifact):
            return resume_stage
    return None


def delete(uuid: str) -> None:
    """Remove the tracker file for *uuid*. No-op if it doesn't exist."""
    p = _path(uuid)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
