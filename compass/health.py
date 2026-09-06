"""Compass health facts — the data behind ``apiary doctor compass``.

Report-only by design. Everything here is a *note*, never an issue: a stale
profile or an empty A/B is a thing the owner may want to know about, not a
broken install, and the doctor's exit code gates CI. ``core/doctor.py``
holds a ten-line adapter; the facts and their thresholds live here so they
can be tested without building a fake registry.

Since D-2026-62 step 1 the capture side is the rule-table pipeline
(``turns/`` -> ``events/`` -> ``rules.md``), so that is what the notes count.
``personality.md``, the A/B and the offline evaluate are still reported while
they remain live; step 2 (T-2026-320) retires them.
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

# D-2026-62 go/no-go: after this many captured sessions, fewer than
# GO_NO_GO_MIN_EVENTS classified events means the pipeline is not earning its
# keep and rules.md stays a hand-maintained seed table.
GO_NO_GO_SESSIONS = 30
GO_NO_GO_MIN_EVENTS = 50

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


def _count_pairs(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def rules_facts(compass: Path, now: float | None = None) -> dict:
    """Counts for the rule-table pipeline under one compass state dir."""
    turns = compass / store.TURNS_DIRNAME
    events = compass / store.EVENTS_DIRNAME
    rules_md = compass / store.RULES_FILENAME

    turn_files = sorted(turns.glob("*.jsonl")) if turns.is_dir() else []
    pairs = sum(_count_pairs(p) for p in turn_files)

    classified = skipped = total_events = 0
    event_sessions: set[str] = set()
    if events.is_dir():
        for path in sorted(events.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            event_sessions.add(path.stem)
            if data.get("skipped"):
                skipped += 1
            else:
                classified += 1
            total_events += len(data.get("events") or [])
    pending = sum(1 for p in turn_files if p.stem not in event_sessions)

    rows = flagged = proposed = None
    rules_age = None
    if rules_md.is_file():
        try:
            text = rules_md.read_text(encoding="utf-8")
        except OSError:
            text = ""
        lines = text.splitlines()
        rows = sum(1 for line in lines if line.startswith("- **"))
        flagged = sum(1 for line in lines if line.startswith("  - evidence:") and "FLAGGED" in line)
        proposed = 0
        in_proposed = False
        for line in lines:
            if line.startswith("## "):
                in_proposed = line.startswith("## Proposed rules")
            elif in_proposed and line.startswith("- ["):
                proposed += 1
        rules_age = _age_days(rules_md, now)

    return {
        "turn_sessions": len(turn_files),
        "pairs": pairs,
        "classified_sessions": classified,
        "skipped_sessions": skipped,
        "pending_sessions": pending,
        "events": total_events,
        "rules_rows": rows,
        "rules_flagged": flagged,
        "rules_proposed": proposed,
        "rules_age_days": rules_age,
    }


def collect(state_dir: Path | None = None, *, now: float | None = None) -> dict:
    """Gather every compass health fact. Never raises."""
    compass = store.compass_dir() if state_dir is None else Path(state_dir) / "compass"
    personality = compass / store.PERSONALITY_FILENAME

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
        **rules_facts(compass, now),
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
        f"turns: {facts['turn_sessions']} session(s), {facts['pairs']} pair(s) captured; "
        f"events: {facts['classified_sessions']} classified, {facts['skipped_sessions']} skipped, "
        f"{facts['pending_sessions']} pending -> {facts['events']} event(s)"
    ]
    if facts["turn_sessions"] >= GO_NO_GO_SESSIONS:
        verdict = "GO" if facts["events"] >= GO_NO_GO_MIN_EVENTS else "NO-GO"
        notes.append(
            f"go/no-go (D-2026-62): {facts['turn_sessions']} sessions captured, "
            f"{facts['events']} events -> {verdict} (threshold {GO_NO_GO_MIN_EVENTS} events "
            f"at {GO_NO_GO_SESSIONS} sessions); record the decision"
        )
    else:
        notes.append(
            f"go/no-go (D-2026-62): decide after {GO_NO_GO_SESSIONS} captured sessions "
            f"({facts['turn_sessions']} so far); keep the pipeline only if events >= "
            f"{GO_NO_GO_MIN_EVENTS} by then"
        )

    if facts["rules_rows"] is None:
        notes.append("rules.md: not written yet (run `compass/rules.py build --write`)")
    else:
        notes.append(
            f"rules.md: {facts['rules_rows']} rows, {facts['rules_flagged']} flagged, "
            f"{facts['rules_proposed']} proposed, rebuilt {facts['rules_age_days']:.0f} day(s) ago"
        )

    if facts["profile_chars"] is None:
        notes.append(
            "personality.md: not written yet (run `/compass-sync` once observations exist)"
        )
    else:
        stale = " — STALE" if facts["stale"] else ""
        notes.append(
            f"personality.md: {facts['profile_chars']} chars, last synthesis "
            f"{facts['profile_age_days']:.0f} day(s) ago{stale} "
            f"(warn above {STALE_SYNTHESIS_DAYS}; retired by T-2026-320)"
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
