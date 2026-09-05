#!/usr/bin/env python3
"""Turn transcript load into measured percent-of-limit.

``budgeter/bill.py`` weights tokens with API list-price ratios because the
subscription's limit formula is not public. This tool replaces the guess with
a measurement: it pairs consecutive usage samples (``budgeter/lib/
usage_samples.py``) inside one limit window, sums the transcript load that
happened between them, and fits ``percent per unit of load``. It also shows
the sessions that drew on the window currently open, each with its estimated
share, and how much of the movement no local session explains — usage from
claude.ai, another device, or the Chrome extension shows up there.

Usage::

    python budgeter/usage_calibrate.py                    # 5-hour window, last 7 days of samples
    python budgeter/usage_calibrate.py --window seven_day
    python budgeter/usage_calibrate.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from budgeter.lib import transcripts, usage_samples
from budgeter.lib.logger import load_config

WINDOW_LENGTH = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate transcript load against the sampled usage limits",
        epilog=(
            "Needs samples in budgeter/data/usage_samples.jsonl, written by the budgeter "
            "Stop hook and the GUI every usage_sample_interval_seconds."
        ),
    )
    parser.add_argument(
        "--window",
        choices=sorted(WINDOW_LENGTH),
        default="five_hour",
        help="Which limit window to calibrate (default five_hour)",
    )
    parser.add_argument(
        "--since",
        default="7d",
        help="How far back to read samples and transcripts: 7d, 36h, or an ISO date (default 7d)",
    )
    parser.add_argument("--top", type=int, default=10, help="Sessions to list for the open window")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument(
        "--samples", default=None, help="Samples file (default budgeter/data/usage_samples.jsonl)"
    )
    parser.add_argument(
        "--projects-dir",
        default=None,
        help="Transcript root to read (default ~/.claude/projects); mainly for tests",
    )
    return parser


def build_intervals(samples: list, window: str) -> list:
    """Consecutive sample pairs inside one window: same ``resets_at`` and a
    non-decreasing utilization. A reset between two samples breaks the pair."""
    out = []
    prev = None
    for rec in samples:
        data = rec.get(window)
        if not isinstance(data, dict) or data.get("utilization") is None:
            prev = None
            continue
        if prev is not None:
            same_window = data.get("resets_at") == prev[window].get("resets_at")
            delta = float(data["utilization"]) - float(prev[window]["utilization"])
            if same_window and delta >= 0 and rec["_ts"] > prev["_ts"]:
                out.append({"t0": prev["_ts"], "t1": rec["_ts"], "delta": delta})
        prev = rec
    return out


def _session_load_between(sessions, weights, t0, t1) -> tuple:
    """Total load and per-session load for calls with t0 < ts <= t1."""
    per_session: dict = {}
    total = 0.0
    for session in sessions:
        for call in session.calls:
            if call.ts <= t0 or call.ts > t1:
                continue
            load = weights.load(call.model, call.tokens)
            total += load
            per_session[session.key] = per_session.get(session.key, 0.0) + load
    return total, per_session


def attribute(intervals: list, sessions, weights) -> dict:
    """Attach transcript load to each interval and fit percent per load.

    Intervals with no local load but a positive delta are usage this machine
    did not produce; their sum is reported as ``unattributed_pct``.
    """
    fitted = []
    sum_delta = 0.0
    sum_load = 0.0
    unattributed = 0.0
    for iv in intervals:
        load, _ = _session_load_between(sessions, weights, iv["t0"], iv["t1"])
        row = dict(iv, load=load)
        if load > 0:
            sum_delta += iv["delta"]
            sum_load += load
        elif iv["delta"] > 0:
            unattributed += iv["delta"]
        fitted.append(row)
    pct_per_load = (sum_delta / sum_load) if sum_load > 0 else None
    return {
        "intervals": fitted,
        "pct_per_load": pct_per_load,
        "fitted_delta_pct": sum_delta,
        "fitted_load": sum_load,
        "unattributed_pct": unattributed,
    }


def open_window(samples: list, window: str, sessions, weights, pct_per_load, top: int) -> dict:
    """The window the latest sample belongs to, and who drew on it."""
    latest = None
    for rec in reversed(samples):
        data = rec.get(window)
        if isinstance(data, dict) and data.get("utilization") is not None:
            latest = rec
            break
    if latest is None:
        return {}
    resets_at = transcripts.parse_ts(latest[window].get("resets_at"))
    if resets_at is None:
        return {}
    start = resets_at - WINDOW_LENGTH[window]
    _, per_session = _session_load_between(sessions, weights, start, latest["_ts"])
    labels = {s.key: s for s in sessions}
    rows = []
    for key, load in sorted(per_session.items(), key=lambda kv: -kv[1])[:top]:
        s = labels[key]
        rows.append(
            {
                "session": key,
                "project": s.label,
                "kind": s.kind,
                "load": round(load, 4),
                "est_pct": round(load * pct_per_load, 2) if pct_per_load else None,
                "first_prompt": s.first_prompt[:70],
            }
        )
    return {
        "sampled_at": latest["_ts"].isoformat(),
        "utilization": latest[window]["utilization"],
        "resets_at": resets_at.isoformat(),
        "window_start": start.isoformat(),
        "sessions": rows,
    }


def run(args) -> dict:
    now = datetime.now(timezone.utc)
    since = transcripts.parse_since(args.since, now)
    samples_path = Path(args.samples) if args.samples else None
    samples = list(usage_samples.iter_samples(samples_path, since))
    weights = transcripts.Weights.from_config(load_config())
    projects_dir = Path(args.projects_dir) if args.projects_dir else None
    sessions = list(transcripts.iter_sessions(since, None, projects_dir))
    intervals = build_intervals(samples, args.window)
    fit = attribute(intervals, sessions, weights)
    current = open_window(samples, args.window, sessions, weights, fit["pct_per_load"], args.top)
    return {
        "window": args.window,
        "since": since.isoformat(),
        "samples": len(samples),
        "sessions": len(sessions),
        "fit": fit,
        "open_window": current,
    }


def render(result: dict) -> str:
    window = result["window"]
    fit = result["fit"]
    lines = [
        f"{window}: {result['samples']} sample(s), {result['sessions']} session(s) since {result['since'][:16]}"
    ]
    if result["samples"] == 0:
        lines.append(
            "no samples yet — the budgeter Stop hook and the GUI record one every "
            "usage_sample_interval_seconds; come back after some activity."
        )
        return "\n".join(lines)
    lines.append("")
    lines.append(f"{'from':16} {'to':16} {'delta%':>7} {'load':>9} {'%/load':>8}")
    for iv in fit["intervals"][-15:]:
        ratio = f"{iv['delta'] / iv['load']:8.3f}" if iv["load"] > 0 else "     n/a"
        lines.append(
            f"{iv['t0'].astimezone().strftime('%m-%d %H:%M'):16} {iv['t1'].astimezone().strftime('%m-%d %H:%M'):16} "
            f"{iv['delta']:7.1f} {iv['load']:9.2f} {ratio}"
        )
    if len(fit["intervals"]) > 15:
        lines.append(f"... {len(fit['intervals']) - 15} earlier interval(s) not shown")
    lines.append("")
    if fit["pct_per_load"] is not None:
        per_pct = 1.0 / fit["pct_per_load"] if fit["pct_per_load"] > 0 else float("inf")
        lines.append(
            f"fit: {fit['pct_per_load']:.3f} % of the {window} limit per unit of load "
            f"({per_pct:,.1f} load per 1%), over {fit['fitted_delta_pct']:.1f}% and {fit['fitted_load']:,.2f} load"
        )
    else:
        lines.append("fit: not enough paired samples with local activity yet")
    if fit["unattributed_pct"] > 0:
        lines.append(
            f"unattributed: {fit['unattributed_pct']:.1f}% moved with no local session active "
            "(claude.ai, another device, the Chrome extension)"
        )
    current = result["open_window"]
    if current:
        lines.append("")
        lines.append(
            f"open window: {current['utilization']:.1f}% at {current['sampled_at'][:16]}, "
            f"resets {current['resets_at'][:16]}"
        )
        lines.append(
            f"{'load':>9} {'est%':>6}  {'project':28} {'kind':11} {'session':8}  first prompt"
        )
        for row in current["sessions"]:
            est = f"{row['est_pct']:6.1f}" if row["est_pct"] is not None else "   n/a"
            lines.append(
                f"{row['load']:9.2f} {est}  {row['project'][:28]:28} {row['kind']:11} "
                f"{row['session'].rsplit('/', 1)[-1][:8]:8}  {row['first_prompt']}"
            )
    return "\n".join(lines)


def _jsonable(result: dict) -> dict:
    out = json.loads(
        json.dumps(result, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))
    )
    return out


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_jsonable(result), indent=2))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
