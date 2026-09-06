#!/usr/bin/env python3
"""Build ``<state-dir>/compass/rules.md`` from seed rows, manual rows and events.

The rule table (D-2026-62) replaces the compass personality profile: one
artifact, written in the second person to Claude, whose rows are scored by
the user's own corrections and acceptances. This module is the aggregation
step and it calls no model — it counts.

Inputs
------
* ``compass/seed_rules.json`` — the shipped seed table (sections, rows,
  self-check). ``source: seed`` until events touch a row.
* ``<state-dir>/compass/rules_manual.json`` — optional hand-added or accepted
  rows, same row schema; a manual row with a seed row's id replaces it.
  ``expiry: "YYYY-MM-DD"`` marks a temporary constraint; expired rows drop.
* ``<state-dir>/compass/events/<sid>.json`` — classified events from
  ``compass/classify.py``: ``{rule, polarity, type, section, action, quote, ts}``.

Aggregation
-----------
Each event weighs ``0.5 ** (age_days / 60)`` (60-day half-life). Per rule:
confirmed and contradicted weight, raw counts, last seen, one quote, and
``confidence = (confirmed + 0.5) / (confirmed + contradicted + 1)`` — a rule
nobody has confirmed or contradicted sits at 0.50. Events on a specific row
also count on its parent principle. A **specific** row whose last two events
both contradict it is *flagged*, not demoted. Three or more events sharing
``(section, action)`` with no rule attached propose a new specific row.

``build`` is a pure function of its inputs and ``now``; with zero events it
reproduces the seed table byte for byte (``--check`` verifies that on disk).

Usage::

    rules.py build [--write] [--check] [--now ISO]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compass import store  # noqa: E402
from core.utils.atomic import write_text_atomic  # noqa: E402
from core.utils.timeutil import parse_iso  # noqa: E402

HALF_LIFE_DAYS = 60.0
FLAG_STREAK = 2  # contradictions in a row that flag a specific row
PROPOSAL_MIN_EVENTS = 3

SECTIONS = ("judgment", "output", "anticipation")
KINDS = ("principle", "specific")
POLARITIES = ("confirm", "contradict")
EVENT_TYPES = ("correction", "acceptance", "anticipation_miss")
SOURCES = ("seed", "mined", "manual", "proposed")

_ID_RE = re.compile(r"^[A-Z]{1,2}\d{1,3}$")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _as_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        return parse_iso(value)
    return None


def load_manual_rows(path: Path | None = None) -> list[dict]:
    """Rows from ``rules_manual.json``; a missing or malformed file is empty."""
    path = store.manual_rules_path() if path is None else Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and _ID_RE.match(str(r.get("id", "")))]


def load_events(folder: Path | None = None) -> list[dict]:
    """Every event in every ``events/<sid>.json``, with ``session_id`` attached."""
    folder = store.events_dir() if folder is None else Path(folder)
    if not folder.is_dir():
        return []
    events: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = str(data.get("session_id") or path.stem)
        for item in data.get("events") or []:
            if isinstance(item, dict):
                events.append({**item, "session_id": sid})
    return events


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def merge_rows(seed_rows: list[dict], manual_rows: list[dict], now: datetime) -> list[dict]:
    """Seed rows with manual overrides applied, expired rows removed.

    A manual row whose id matches a seed row replaces it in place (keeping the
    seed's position); new manual ids append after the seed rows of their
    section. Rows past ``expiry`` are dropped.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for row in seed_rows:
        rid = str(row.get("id"))
        by_id[rid] = {**row, "source": row.get("source") or "seed"}
        order.append(rid)
    for row in manual_rows:
        rid = str(row.get("id"))
        merged = {**row, "source": row.get("source") or "manual"}
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = merged
    today = now.date()
    rows: list[dict] = []
    for rid in order:
        row = by_id[rid]
        expiry = row.get("expiry")
        if expiry:
            try:
                if datetime.fromisoformat(str(expiry)[:10]).date() < today:
                    continue
            except ValueError:
                pass
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def decay_weight(event_ts, now: datetime, half_life_days: float = HALF_LIFE_DAYS) -> float:
    """Exponential decay by age; an undated or future event weighs 1.0."""
    ts = _as_ts(event_ts)
    if ts is None:
        return 1.0
    age_days = (now - ts).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def confidence(confirmed: float, contradicted: float) -> float:
    """Jeffreys-style estimate: 0.50 with no evidence, moving with the weights."""
    return round((confirmed + 0.5) / (confirmed + contradicted + 1.0), 2)


def _new_stats() -> dict:
    return {
        "confirmed": 0.0,
        "contradicted": 0.0,
        "n_confirm": 0,
        "n_contradict": 0,
        "last_seen": None,
        "quote": None,
        "history": [],  # (ts, polarity) for the streak check
        "sessions": set(),
    }


def aggregate(rows: list[dict], events: list[dict], now: datetime) -> dict[str, dict]:
    """Per-rule evidence. Events on a specific row roll up to its parent."""
    parents = {str(r["id"]): r.get("parent") for r in rows}
    stats: dict[str, dict] = {str(r["id"]): _new_stats() for r in rows}

    def touch(rid: str, event: dict, weight: float, ts: datetime | None) -> None:
        s = stats[rid]
        if event["polarity"] == "confirm":
            s["confirmed"] += weight
            s["n_confirm"] += 1
        else:
            s["contradicted"] += weight
            s["n_contradict"] += 1
        s["history"].append((ts, event["polarity"]))
        s["sessions"].add(event.get("session_id"))
        if ts is not None and (s["last_seen"] is None or ts >= s["last_seen"]):
            s["last_seen"] = ts
            if event.get("quote"):
                s["quote"] = str(event["quote"])
        elif s["quote"] is None and event.get("quote"):
            s["quote"] = str(event["quote"])

    for event in events:
        rid = event.get("rule")
        if rid not in stats or event.get("polarity") not in POLARITIES:
            continue
        ts = _as_ts(event.get("ts"))
        weight = decay_weight(ts, now)
        touch(rid, event, weight, ts)
        parent = parents.get(rid)
        if parent in stats and parent != rid:
            touch(parent, event, weight, ts)

    for rid, s in stats.items():
        s["confidence"] = confidence(s["confirmed"], s["contradicted"])
        s["history"].sort(key=lambda h: (h[0] is None, h[0] or now))
        s["n_sessions"] = len(s["sessions"])
    return stats


def flagged_rules(rows: list[dict], stats: dict[str, dict]) -> list[str]:
    """Specific rows whose last ``FLAG_STREAK`` events all contradict them."""
    flagged: list[str] = []
    for row in rows:
        if row.get("kind") != "specific":
            continue
        history = stats.get(str(row["id"]), {}).get("history") or []
        tail = [pol for _, pol in history[-FLAG_STREAK:]]
        if len(tail) == FLAG_STREAK and all(p == "contradict" for p in tail):
            flagged.append(str(row["id"]))
    return flagged


def normalize_action(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower())
    return " ".join(text.split())


def proposals(events: list[dict], rows: list[dict]) -> list[dict]:
    """New specific rows suggested by unattached events sharing (section, action)."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for event in events:
        if event.get("rule"):
            continue
        section = event.get("section")
        action = normalize_action(event.get("action") or "")
        if section not in SECTIONS or not action:
            continue
        groups.setdefault((section, action), []).append(event)
    out: list[dict] = []
    for (section, action), group in sorted(groups.items()):
        if len(group) < PROPOSAL_MIN_EVENTS:
            continue
        quote = next((str(e["quote"]) for e in group if e.get("quote")), None)
        out.append(
            {
                "section": section,
                "action": action,
                "events": len(group),
                "sessions": len({e.get("session_id") for e in group}),
                "quote": quote,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_day(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d") if ts else "never"


def _render_row(row: dict, stat: dict, flagged: bool) -> list[str]:
    rid = row["id"]
    kind = row.get("kind", "specific")
    label = "principle" if kind == "principle" else f"specific, {row.get('parent') or '-'}"
    lines = [f"- **{rid}** ({label}) {row['rule'].strip()}"]
    if row.get("why"):
        lines.append(f"  - why: {row['why'].strip()}")
    source = row.get("source") or "seed"
    if source == "seed" and (stat["n_confirm"] or stat["n_contradict"]):
        source = "mined"
    evidence = (
        f"  - evidence: confirmed {stat['n_confirm']}, contradicted {stat['n_contradict']}, "
        f"last seen {_fmt_day(stat['last_seen'])}; confidence {stat['confidence']:.2f}; "
        f"source {source}"
    )
    if row.get("expiry"):
        evidence += f"; expires {str(row['expiry'])[:10]}"
    if flagged:
        evidence += "; FLAGGED"
    lines.append(evidence)
    if stat.get("quote"):
        quote = " ".join(str(stat["quote"]).split())
        lines.append(f'  - quote: "{quote}"')
    return lines


def render(
    seed: dict,
    rows: list[dict],
    stats: dict[str, dict],
    events: list[dict],
    proposed: list[dict],
    flagged: list[str],
) -> str:
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    latest = max((t for t in (_as_ts(e.get("ts")) for e in events) if t), default=None)
    out: list[str] = [
        "# Rules for Claude",
        "",
        "Second-person rules mined from this user's corrections and acceptances "
        "(D-2026-62). They shape how you reason about recommendations and how you "
        "write, throughout a session, not only at tool boundaries. Explicit feedback "
        "in memory overrides any row here.",
        "",
        f"Built by `compass/rules.py build` from {len(events)} event(s) across "
        f"{len(sessions)} session(s); last event {_fmt_day(latest)}. Generated file: "
        "add or override rows in `rules_manual.json`, not here.",
        "",
    ]
    for section in seed.get("sections", []):
        sid = section["id"]
        out.append(f"## {section['title']} - {section['subtitle']}")
        out.append("")
        for row in rows:
            if row.get("section") != sid:
                continue
            out.extend(_render_row(row, stats[str(row["id"])], row["id"] in flagged))
        out.append("")
    if flagged:
        out.append("## Flags")
        out.append("")
        out.append(
            f"Specific rows contradicted {FLAG_STREAK} times in a row. Not demoted: "
            "check the quotes and decide."
        )
        out.extend(f"- {rid}" for rid in flagged)
        out.append("")
    if proposed:
        out.append("## Proposed rules")
        out.append("")
        out.append(
            f"{PROPOSAL_MIN_EVENTS}+ events share an action with no covering rule. "
            "Accept one by adding a row to `rules_manual.json`."
        )
        for p in proposed:
            line = (
                f"- [{p['section']}] {p['action']} ({p['events']} events, {p['sessions']} sessions)"
            )
            if p.get("quote"):
                line += f' - "{" ".join(str(p["quote"]).split())}"'
            out.append(line)
        out.append("")
    check = seed.get("self_check") or {}
    if check.get("items"):
        out.append(f"## {check.get('title', 'Self-check')}")
        out.append("")
        out.extend(f"{i}. {item}" for i, item in enumerate(check["items"], 1))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(
    *,
    seed: dict | None = None,
    manual_rows: list[dict] | None = None,
    events: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """Pure build. Returns ``{"text", "rows", "stats", "flagged", "proposed", "events"}``."""
    now = now or datetime.now(timezone.utc)
    seed = store.load_seed_rules() if seed is None else seed
    manual_rows = load_manual_rows() if manual_rows is None else manual_rows
    events = load_events() if events is None else events
    rows = merge_rows(list(seed.get("rules", [])), manual_rows, now)
    stats = aggregate(rows, events, now)
    flagged = flagged_rules(rows, stats)
    proposed = proposals(events, rows)
    text = render(seed, rows, stats, events, proposed, flagged)
    return {
        "text": text,
        "rows": rows,
        "stats": stats,
        "flagged": flagged,
        "proposed": proposed,
        "events": len(events),
    }


def rule_ids(seed: dict | None = None, manual_rows: list[dict] | None = None) -> list[str]:
    """Active rule ids — the classifier's vocabulary."""
    now = datetime.now(timezone.utc)
    seed = store.load_seed_rules() if seed is None else seed
    manual_rows = load_manual_rows() if manual_rows is None else manual_rows
    return [str(r["id"]) for r in merge_rows(list(seed.get("rules", [])), manual_rows, now)]


def cmd_build(args: argparse.Namespace) -> int:
    now = _as_ts(args.now) if args.now else None
    if args.now and now is None:
        print(f"--now is not ISO-8601: {args.now!r}", file=sys.stderr)
        return 2
    result = build(now=now)
    target = store.rules_path()
    if args.check:
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            print(f"{target} does not exist; run `rules.py build --write`", file=sys.stderr)
            return 1
        if current != result["text"]:
            print(f"{target} is out of date; run `rules.py build --write`", file=sys.stderr)
            return 1
        print(f"ok - {target} matches ({len(result['rows'])} rows, {result['events']} events)")
        return 0
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(target, result["text"])
        print(
            f"wrote {target} ({len(result['rows'])} rows, {result['events']} events, "
            f"{len(result['flagged'])} flagged, {len(result['proposed'])} proposed)"
        )
        return 0
    sys.stdout.write(result["text"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regenerate the compass rule table from seed rows, manual rows and events"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build", help="Render rules.md (stdout unless --write)")
    p_build.add_argument("--write", action="store_true", help="Write <state-dir>/compass/rules.md")
    p_build.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the on-disk rules.md differs from a fresh build",
    )
    p_build.add_argument("--now", help="ISO-8601 instant for decay math (default: now)")
    p_build.set_defaults(func=cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
