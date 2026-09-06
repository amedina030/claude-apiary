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

Delivery (D-2026-62 step 2) reads the rendered ``rules.md`` back rather than
rebuilding: :func:`parse_rules_md` recovers the rows and the self-check from
the generated text, :func:`pin_text` renders the per-turn pin (principle rows
plus self-check) and :func:`rule_line` one row for the hook-point injections.
The row format is therefore part of the contract — change ``_render_row`` and
``parse_rules_md`` together.

Output heuristics (``compass/heuristics.py``, written by the Stop hook) are a
secondary signal for the output rules only: they are summarised under the
Output section and never counted in a row's confidence.

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
from compass import heuristics, store  # noqa: E402
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


FLAG_MARK = "FLAGGED"
HEURISTICS_SECTION = "output"  # the section the Stop-hook heuristics report under


def _row_label(row: dict, stat: dict, flagged: bool) -> str:
    """``principle; mined 0.83`` / ``specific, J1; seed 0.50; expires ...; FLAGGED``."""
    kind = row.get("kind", "specific")
    label = "principle" if kind == "principle" else f"specific, {row.get('parent') or '-'}"
    source = row.get("source") or "seed"
    if source == "seed" and (stat["n_confirm"] or stat["n_contradict"]):
        source = "mined"
    parts = [label, f"{source} {stat['confidence']:.2f}"]
    if row.get("expiry"):
        parts.append(f"expires {str(row['expiry'])[:10]}")
    if flagged:
        parts.append(FLAG_MARK)
    return "; ".join(parts)


def _render_row(row: dict, stat: dict, flagged: bool) -> list[str]:
    """One row: header, ``why``, and an evidence line only once events exist.

    Every row is delivered to Claude at every session start, so the format is
    the budget: the header carries source and confidence, seed rows with no
    events carry no evidence line, and the quote rides on the evidence line.
    """
    rid = row["id"]
    lines = [f"- **{rid}** ({_row_label(row, stat, flagged)}) {row['rule'].strip()}"]
    if row.get("why"):
        lines.append(f"  - why: {row['why'].strip()}")
    if stat["n_confirm"] or stat["n_contradict"]:
        evidence = (
            f"  - evidence: {stat['n_confirm']} confirmed, {stat['n_contradict']} "
            f"contradicted, last {_fmt_day(stat['last_seen'])}"
        )
        if stat.get("quote"):
            evidence += f'; "{" ".join(str(stat["quote"]).split())}"'
        lines.append(evidence)
    return lines


def _render_heuristics(summary: dict) -> str:
    """The secondary-signal line under the Output section."""

    def pct(key: str) -> str:
        return f"{100.0 * summary[key] / summary['turns']:.0f}%"

    return (
        f"- heuristics (Stop hook, secondary signal, not counted in confidence; "
        f"{summary['turns']} turn(s) in {summary['sessions']} session(s)): "
        f"outcome in the first sentence {pct('outcome_first')}, at most one "
        f"recommendation {pct('one_recommendation')}, length band "
        f"{heuristics.LENGTH_BAND[0]}-{heuristics.LENGTH_BAND[1]} chars {pct('length_band')}"
    )


def render(
    seed: dict,
    rows: list[dict],
    stats: dict[str, dict],
    events: list[dict],
    proposed: list[dict],
    flagged: list[str],
    heuristic_summary: dict | None = None,
) -> str:
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    latest = max((t for t in (_as_ts(e.get("ts")) for e in events) if t), default=None)
    out: list[str] = [
        "# Rules for Claude",
        "",
        "Second-person rules mined from this user's corrections and acceptances "
        "(D-2026-62). They shape how you reason about recommendations and how you "
        "write, throughout a session, not only at tool boundaries. Explicit feedback "
        "in memory overrides any row here. Generated by `compass/rules.py build` from "
        f"{len(events)} event(s) across {len(sessions)} session(s), last event "
        f"{_fmt_day(latest)}; add or override rows in `rules_manual.json`, never here.",
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
        if sid == HEURISTICS_SECTION and heuristic_summary and heuristic_summary.get("turns"):
            out.append(_render_heuristics(heuristic_summary))
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
    heuristic_turns: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """Pure build. Returns ``{"text", "rows", "stats", "flagged", "proposed", "events"}``.

    *heuristic_turns* defaults to the Stop-hook heuristics of every session
    that has an events file, so the summary line only moves when a session is
    classified (which rebuilds this file anyway) and ``--check`` stays honest
    while a session is live.
    """
    now = now or datetime.now(timezone.utc)
    seed = store.load_seed_rules() if seed is None else seed
    manual_rows = load_manual_rows() if manual_rows is None else manual_rows
    events = load_events() if events is None else events
    if heuristic_turns is None:
        heuristic_turns = heuristics.load_classified()
    rows = merge_rows(list(seed.get("rules", [])), manual_rows, now)
    stats = aggregate(rows, events, now)
    flagged = flagged_rules(rows, stats)
    proposed = proposals(events, rows)
    summary = heuristics.summarize(heuristic_turns)
    text = render(seed, rows, stats, events, proposed, flagged, summary)
    return {
        "text": text,
        "rows": rows,
        "stats": stats,
        "flagged": flagged,
        "proposed": proposed,
        "events": len(events),
        "heuristics": summary,
    }


# ---------------------------------------------------------------------------
# Delivery: read the rendered table back
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(r"^- \*\*(?P<id>[A-Z]{1,2}\d{1,3})\*\* \((?P<label>[^)]*)\) (?P<rule>.+)$")
_WHY_PREFIX = "  - why: "
_SELF_CHECK_HEADING = "## Self-check"
_CHECK_ITEM_RE = re.compile(r"^\d+\. (?P<item>.+)$")


def parse_rules_md(text: str) -> dict:
    """Rows and self-check items from a rendered ``rules.md``.

    The inverse of :func:`render` for the parts delivery needs: each row's
    ``id``, ``kind`` (``principle`` | ``specific``), ``parent``, ``rule``,
    ``why`` and whether it is flagged; and the self-check items in order.
    Tolerant of hand edits — an unparseable line is skipped, never fatal.
    """
    rows: list[dict] = []
    items: list[str] = []
    in_check = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("## "):
            in_check = stripped.startswith(_SELF_CHECK_HEADING)
            continue
        if in_check:
            m = _CHECK_ITEM_RE.match(stripped)
            if m:
                items.append(m.group("item").strip())
            continue
        m = _ROW_RE.match(stripped)
        if m:
            parts = [p.strip() for p in m.group("label").split(";")]
            head = parts[0] if parts else ""
            kind = "principle" if head == "principle" else "specific"
            parent = None
            if kind == "specific" and "," in head:
                parent = head.split(",", 1)[1].strip() or None
                if parent == "-":
                    parent = None
            rows.append(
                {
                    "id": m.group("id"),
                    "kind": kind,
                    "parent": parent,
                    "rule": m.group("rule").strip(),
                    "why": "",
                    "flagged": FLAG_MARK in parts,
                }
            )
            continue
        if rows and stripped.startswith(_WHY_PREFIX) and not rows[-1]["why"]:
            rows[-1]["why"] = stripped[len(_WHY_PREFIX) :].strip()
    return {"rows": rows, "self_check": items}


def pin_text(parsed: dict) -> str:
    """The per-turn pin: principle rows plus the self-check, one line each.

    Injected by ``core/hooks/compass_rules.py`` on every user message after the
    first (the startup block carries the whole table), so the rules sit near
    the active turn and survive compaction. Roughly 200 tokens.
    """
    principles = [r for r in parsed.get("rows", []) if r.get("kind") == "principle"]
    lines = [
        "compass rules pin (full table in the startup block; explicit user statements "
        "and feedback memory override):"
    ]
    lines.extend(f"{r['id']} {r['rule']}" for r in principles)
    items = parsed.get("self_check") or []
    if items:
        lines.append("Self-check before finalizing: " + " ".join(items))
    return "\n".join(lines)


def rule_line(parsed: dict, rule_id: str) -> str | None:
    """``J5 rule text (why: ...)`` for one row, or ``None`` if the id is absent."""
    for row in parsed.get("rows", []):
        if row.get("id") == rule_id:
            line = f"{row['id']} {row['rule']}"
            if row.get("why"):
                line += f" (why: {row['why']})"
            return line
    return None


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
            f"{len(result['flagged'])} flagged, {len(result['proposed'])} proposed, "
            f"{result['heuristics']['turns']} heuristic turns)"
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
