"""Compass health facts — the data behind ``apiary doctor compass``.

Report-only by design. Everything here is a *note*, never an issue: an
unclassified session or a pending go/no-go is a thing the owner may want to
know about, not a broken install, and the doctor's exit code gates CI.
``core/doctor.py`` holds a ten-line adapter; the facts and their thresholds
live here so they can be tested without building a fake registry.

What is counted is the rule-table pipeline (D-2026-62): ``turns/`` ->
``events/`` -> ``rules.md``, plus the Stop-hook output heuristics beside the
events, and the 30-session go/no-go on the capture automation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from compass import rules, store

# D-2026-62 go/no-go: after this many captured sessions, fewer than
# GO_NO_GO_MIN_EVENTS classified events means the pipeline is not earning its
# keep and rules.md stays a hand-maintained seed table.
GO_NO_GO_SESSIONS = 30
GO_NO_GO_MIN_EVENTS = 50


def _age_days(path: Path, now: float | None = None) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return ((time.time() if now is None else now) - mtime) / 86400


def _count_lines(path: Path) -> int:
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
    pairs = sum(_count_lines(p) for p in turn_files)

    classified = skipped = total_events = 0
    heuristic_turns = 0
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
        heuristic_turns = sum(_count_lines(p) for p in events.glob(f"*{store.HEURISTICS_SUFFIX}"))
    pending = sum(1 for p in turn_files if p.stem not in event_sessions)

    rows = flagged = proposed = None
    rules_age = None
    if rules_md.is_file():
        try:
            text = rules_md.read_text(encoding="utf-8")
        except OSError:
            text = ""
        parsed = rules.parse_rules_md(text)
        rows = len(parsed["rows"])
        flagged = sum(1 for row in parsed["rows"] if row.get("flagged"))
        proposed = 0
        in_proposed = False
        for line in text.splitlines():
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
        "heuristic_turns": heuristic_turns,
        "rules_rows": rows,
        "rules_flagged": flagged,
        "rules_proposed": proposed,
        "rules_age_days": rules_age,
    }


def collect(state_dir: Path | None = None, *, now: float | None = None) -> dict:
    """Gather every compass health fact. Never raises."""
    compass = store.compass_dir() if state_dir is None else Path(state_dir) / "compass"
    return {
        "compass_dir": str(compass),
        "exists": compass.is_dir(),
        **rules_facts(compass, now),
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
        f"{facts['pending_sessions']} pending -> {facts['events']} event(s); "
        f"{facts['heuristic_turns']} heuristic turn(s)"
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
    return notes
