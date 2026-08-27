"""Compass health facts — the data behind ``apiary doctor compass``.

Report-only by design. Everything here is a *note*, never an issue: a stale
profile or an empty A/B is a thing the owner may want to know about, not a
broken install, and the doctor's exit code gates CI. ``core/doctor.py``
holds a ten-line adapter; the facts and their thresholds live here so they
can be tested without building a fake registry.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from compass import ab, store

# Beyond this the profile is describing a user who has moved on. Matches the
# threshold review §5a-H.3 asked for.
STALE_SYNTHESIS_DAYS = 14

# core.session sweeps identity files older than this, so the arm counts are a
# rolling window, not a lifetime total. Named here so the note can say so.
IDENTITY_WINDOW_DAYS = 30

SESSIONS_DIRNAME = "sessions"


def _age_days(path: Path, now: float | None = None) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return ((time.time() if now is None else now) - mtime) / 86400


def arm_counts(state_dir: Path) -> dict[str, int]:
    """Count recorded ``compass_arm`` values in the session identity files.

    Identity files that predate the A/B (or were written while it was off)
    have no arm; those are counted under ``unrecorded``.
    """
    counts = {arm: 0 for arm in ab.ARMS}
    counts["unrecorded"] = 0
    sessions = Path(state_dir) / SESSIONS_DIRNAME
    if not sessions.is_dir():
        return counts
    for path in sorted(sessions.glob("identity-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        arm = data.get(ab.IDENTITY_ARM_KEY) if isinstance(data, dict) else None
        if arm in counts:
            counts[arm] += 1
        else:
            counts["unrecorded"] += 1
    return counts


def collect(state_dir: Path | None = None, *, now: float | None = None) -> dict:
    """Gather every compass health fact. Never raises."""
    compass = store.compass_dir() if state_dir is None else Path(state_dir) / "compass"
    observations = compass / store.OBSERVATIONS_DIRNAME
    archive = observations / store.ARCHIVE_DIRNAME
    personality = compass / store.PERSONALITY_FILENAME

    active = 0
    if observations.is_dir():
        active = sum(1 for p in observations.iterdir() if p.is_file() and p.suffix == ".json")
    archived = len(list(archive.rglob("*.json"))) if archive.is_dir() else 0

    profile_chars = None
    profile_age = None
    if personality.is_file():
        try:
            profile_chars = len(personality.read_text(encoding="utf-8"))
        except OSError:
            profile_chars = None
        profile_age = _age_days(personality, now)

    cached = None
    last = compass / "evaluate" / "last.json"
    try:
        data = json.loads(last.read_text(encoding="utf-8"))
        cached = data if isinstance(data, dict) else None
    except (OSError, ValueError):
        cached = None

    config = ab.load_config()
    # Identity files live beside compass/ under the same state dir.
    counts = arm_counts(compass.parent)

    return {
        "compass_dir": str(compass),
        "exists": compass.is_dir(),
        "active_observations": active,
        "archived_observations": archived,
        "profile_chars": profile_chars,
        "profile_age_days": profile_age,
        "stale": profile_age is not None and profile_age > STALE_SYNTHESIS_DAYS,
        "ab_enabled": bool(config.get("ab_enabled")),
        "arm_counts": counts,
        "last_evaluate": cached,
    }


def format_notes(facts: dict) -> list[str]:
    """Render :func:`collect`'s facts as doctor notes (report-only)."""
    if not facts.get("exists"):
        return [
            f"no compass state at {facts['compass_dir']} — nothing captured for this target yet"
        ]

    notes = [
        f"observations: {facts['active_observations']} active, "
        f"{facts['archived_observations']} archived"
    ]

    if facts["profile_chars"] is None:
        notes.append(
            "personality.md: not written yet (run `/compass-sync` once observations exist)"
        )
    else:
        stale = " — STALE" if facts["stale"] else ""
        notes.append(
            f"personality.md: {facts['profile_chars']} chars, last synthesis "
            f"{facts['profile_age_days']:.0f} day(s) ago{stale} "
            f"(warn above {STALE_SYNTHESIS_DAYS})"
        )

    counts = facts["arm_counts"]
    if facts["ab_enabled"]:
        notes.append(
            f"A/B: enabled — arms on={counts['on']} off={counts['off']} "
            f"(+{counts['unrecorded']} sessions with no arm recorded; identity "
            f"files are swept after {IDENTITY_WINDOW_DAYS} days, so this is a "
            f"rolling window)"
        )
    else:
        notes.append(
            "A/B: disabled (compass/config.json ab_enabled=false) — "
            "every session gets the profile; see docs/compass-measurement.md"
        )

    cached = facts["last_evaluate"]
    if not cached:
        notes.append("evaluate: never run — `compass/evaluate.py offline` writes the headline here")
    else:
        headline = cached.get("headline")
        lift = cached.get("lift_over_majority")
        notes.append(
            "evaluate: headline "
            f"{'n/a' if headline is None else f'{headline * 100:.1f}%'}, "
            f"lift {'n/a' if lift is None else f'{lift * 100:+.1f} pts'} "
            f"over majority ({cached.get('mode', '?')} synthesiser, "
            f"{cached.get('folds', '?')} folds, computed "
            f"{str(cached.get('computed_at', '?'))[:10]})"
        )
    return notes
