"""Path resolution, dimension config, and observation helpers for compass.

Compass state lives at ``<state-dir>/compass/`` where ``<state-dir>`` is
the per-target directory resolved by the centralized state registry
(``<apiary>/.repos/<name>-<id>/``). The launcher exports
``APIARY_TARGET_STATE_DIR`` after registry lookup so this module can
return the correct path without re-running the resolver. When that env
var is unset (direct invocation, tests, or unmigrated targets), we fall
back to the legacy in-repo path ``<git-repo-root>/.apiary/compass/``.

Layout::

    observations/<session_id>.json   — active per-session observation files
    observations/archive/<year>-<week>/<session_id>.json — archived
    personality.md                   — synthesized profile, injected at startup
    corrections.md                   — manual high-weight evidence for synthesizer

Dimensions config ships with the compass module at ``compass/dimensions.json``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Path resolution and the two names that describe it live in core.utils.state -
# re-exported here so callers (and tests) can keep saying ``store.<NAME>``
# while there is exactly one definition in the tree (review X-3).
from core.utils.state import (  # noqa: F401
    LEGACY_STATE_DIRNAME as APIARY_STATE_DIRNAME,
)
from core.utils.state import (
    TARGET_STATE_DIR_ENV,  # noqa: F401 — re-export: compass.evaluate and the
    # compass tests read it as compass.store.TARGET_STATE_DIR_ENV.
    resolve_state_dir,
)

COMPASS_SUBDIR = "compass"

OBSERVATIONS_DIRNAME = "observations"
ARCHIVE_DIRNAME = "archive"

PERSONALITY_FILENAME = "personality.md"
CORRECTIONS_FILENAME = "corrections.md"

DIMENSIONS_FILE = Path(__file__).resolve().parent / "dimensions.json"

# Bloat thresholds (per spec C-2026-30).
ARCHIVE_MIN_ACTIVE = 50          # never archive when active count is below this
ARCHIVE_MAX_AGE_DAYS = 90        # files older than this become archive candidates

VALID_VOLATILITY = frozenset({"stable", "volatile"})

# 8-char hex prefix or full UUID; matches the SessionId convention.
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$|^[0-9a-f]{8}$")


def compass_dir(start: Path | None = None) -> Path:
    """Return the compass state directory.

    Delegates to :func:`core.utils.state.resolve_state_dir`, which is the
    one place the precedence lives (launcher env var → the repo's pins →
    the legacy ``.apiary/pointer`` breadcrumb → ``<repo>/.apiary/`` →
    ``<cwd>/.apiary/`` outside a git repo). See its docstring.
    """
    return resolve_state_dir(start, subdir=COMPASS_SUBDIR, cwd_fallback=True)


def observations_dir(start: Path | None = None) -> Path:
    return compass_dir(start) / OBSERVATIONS_DIRNAME


def archive_dir(start: Path | None = None) -> Path:
    return observations_dir(start) / ARCHIVE_DIRNAME


def personality_path(start: Path | None = None) -> Path:
    return compass_dir(start) / PERSONALITY_FILENAME


def corrections_path(start: Path | None = None) -> Path:
    return compass_dir(start) / CORRECTIONS_FILENAME


def observation_path(session_id: str, start: Path | None = None) -> Path:
    """Path for a session's active observation file."""
    short = session_id.split("-", 1)[0][:8].lower()
    return observations_dir(start) / f"{short}.json"


def ensure_layout(start: Path | None = None) -> None:
    """Create compass/observations/ and the archive subdir if missing."""
    archive_dir(start).mkdir(parents=True, exist_ok=True)


def load_dimensions() -> dict:
    """Return the full dimensions config as a dict."""
    return json.loads(DIMENSIONS_FILE.read_text(encoding="utf-8"))


def dimension_names() -> list[str]:
    return [d["name"] for d in load_dimensions()["dimensions"]]


def volatile_dimension_names() -> list[str]:
    return [d["name"] for d in load_dimensions()["dimensions"] if d.get("volatile")]


def list_active_observations(start: Path | None = None) -> list[Path]:
    """Return active (non-archived) observation files, sorted by mtime descending."""
    obs = observations_dir(start)
    if not obs.exists():
        return []
    files = [p for p in obs.iterdir() if p.is_file() and p.suffix == ".json"]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def count_active_observations(start: Path | None = None) -> int:
    return len(list_active_observations(start))


def validate_observation(data: dict, *, valid_dimensions: set[str] | None = None,
                         expected_session_id: str | None = None) -> list[str]:
    """Validate an observation file payload. Returns a list of error messages.

    When *expected_session_id* is given, the payload's session_id must also
    match it (after lowercase + 8-char prefix comparison) — guards against
    LLM-generated payloads that hallucinate a different hex string.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["payload is not a JSON object"]

    sid = data.get("session_id")
    if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
        errors.append("session_id missing or not a valid 8-char hex / UUID")
    elif expected_session_id is not None:
        expected_short = expected_session_id.split("-", 1)[0][:8].lower()
        actual_short = sid.split("-", 1)[0][:8].lower()
        if expected_short != actual_short:
            errors.append(
                f"session_id {sid!r} does not match expected {expected_session_id!r}"
            )

    captured = data.get("captured_at")
    if not isinstance(captured, str):
        errors.append("captured_at missing or not a string")
    else:
        try:
            datetime.fromisoformat(captured.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"captured_at is not ISO-8601: {captured!r}")

    obs = data.get("observations")
    if not isinstance(obs, list):
        errors.append("observations missing or not a list")
        return errors

    if valid_dimensions is None:
        valid_dimensions = set(dimension_names())

    for i, item in enumerate(obs):
        if not isinstance(item, dict):
            errors.append(f"observations[{i}] is not an object")
            continue
        dim = item.get("dimension")
        if dim not in valid_dimensions:
            errors.append(f"observations[{i}].dimension {dim!r} not in configured dimensions")
        if not isinstance(item.get("observation"), str) or not item["observation"].strip():
            errors.append(f"observations[{i}].observation missing or empty")
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            errors.append(f"observations[{i}].evidence missing or empty")
        vol = item.get("volatility")
        if vol not in VALID_VOLATILITY:
            errors.append(f"observations[{i}].volatility must be one of {sorted(VALID_VOLATILITY)}")

    return errors


def load_observation(path: Path) -> dict | None:
    """Read and validate a single observation file. Returns None on failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if validate_observation(data):
        return None
    return data


def archive_target_path(file_path: Path, *, now: datetime | None = None,
                        start: Path | None = None) -> Path:
    """Compute the archive destination for a file, organized by ISO year-week."""
    now = now or datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return archive_dir(start) / f"{iso_year}-{iso_week:02d}" / file_path.name
