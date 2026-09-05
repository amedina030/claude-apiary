#!/usr/bin/env python3
"""Where the usage went: every Claude Code call on this machine, attributed.

Reads the Claude Code transcripts under ``~/.claude/projects`` (see
``budgeter/lib/transcripts.py``) and groups the billed tokens of every call
by project, session, model, day or kind (interactive vs headless). Covers
interactive sessions, subagents and every ``claude -p`` the runner and the
scheduled pipelines make, whether or not a repo has apiary hooks installed.

``load`` is tokens weighted by the ``model_weights`` table in
``budgeter/config.json`` — Anthropic API list-price ratios, used only so a
Sonnet call and a Fable call are comparable. It is not a bill; the account has
no API key. Run ``budgeter/usage_calibrate.py`` to turn load into measured
percent-of-limit once usage samples exist.

Usage::

    python budgeter/bill.py                       # last 7 days by project
    python budgeter/bill.py --since 24h --by session
    python budgeter/bill.py --by model --json
    python budgeter/bill.py --project job_search --by session --top 40
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from budgeter.lib import transcripts
from budgeter.lib.logger import load_config

GROUPINGS = ("project", "session", "model", "day", "kind")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Attribute Claude Code usage from the machine-wide transcripts",
        epilog=(
            "load = tokens weighted by budgeter/config.json model_weights (API list-price "
            "ratios, relative only; not a bill). Cache reads are weighted at "
            "cache_read_factor of input, cache writes at cache_write_factor."
        ),
    )
    parser.add_argument(
        "--since",
        default="7d",
        help="Window start: a duration (7d, 36h, 90m) or an ISO date/datetime (default 7d)",
    )
    parser.add_argument(
        "--until", default=None, help="Window end as ISO date/datetime (default now)"
    )
    parser.add_argument(
        "--by",
        choices=GROUPINGS,
        default="project",
        help="Group by project (default), session, model, day or kind (interactive/headless)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Only sessions whose project label contains this text (case-insensitive)",
    )
    parser.add_argument("--top", type=int, default=25, help="Rows to print (default 25)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    parser.add_argument(
        "--projects-dir",
        default=None,
        help="Transcript root to read (default ~/.claude/projects); mainly for tests",
    )
    return parser


def _fmt_m(n: int) -> str:
    return f"{n / 1e6:7.2f}M"


def collect(args) -> dict:
    """Run the walk and aggregation the CLI prints; shared with tests."""
    now = datetime.now(timezone.utc)
    since = transcripts.parse_since(args.since, now)
    until = transcripts.parse_since(args.until, now) if args.until else None
    weights = transcripts.Weights.from_config(load_config())
    projects_dir = Path(args.projects_dir) if args.projects_dir else None
    sessions = list(transcripts.iter_sessions(since, until, projects_dir))
    if args.project:
        needle = args.project.lower()
        sessions = [s for s in sessions if needle in s.label.lower()]
    buckets = transcripts.aggregate(sessions, weights, by=args.by)
    unweighted = buckets.pop("__unweighted__", [])
    rows = sorted(buckets.values(), key=lambda b: -b.load)
    total_load = sum(b.load for b in rows)
    total_tokens = sum(transcripts.total_tokens(b.tokens) for b in rows)
    return {
        "since": since.isoformat(),
        "until": (until or now).isoformat(),
        "by": args.by,
        "sessions": len(sessions),
        "total_load": round(total_load, 4),
        "total_tokens": total_tokens,
        "unweighted_models": list(unweighted),
        "rows": rows,
    }


def render(result: dict, top: int) -> str:
    rows = result["rows"][:top]
    total_load = result["total_load"] or 0.0
    lines = [
        f"window {result['since'][:16]} -> {result['until'][:16]}  "
        f"sessions {result['sessions']}  load {total_load:,.2f}  tokens {_fmt_m(result['total_tokens']).strip()}",
        "",
    ]
    by = result["by"]
    if by == "session":
        lines.append(
            f"{'load':>9} {'share':>6}  {'project':28} {'kind':11} {'models':24} {'session':8}  first prompt"
        )
        for b in rows:
            share = 100 * b.load / total_load if total_load else 0.0
            kind = "headless" if b.kinds["headless"] >= b.kinds["interactive"] else "interactive"
            models = ",".join(sorted(m.replace("claude-", "") for m in b.models))[:24]
            lines.append(
                f"{b.load:9.2f} {share:5.1f}%  {b.label[:28]:28} {kind:11} {models:24} "
                f"{b.name.rsplit('/', 1)[-1][:8]:8}  {b.first_prompt[:70]}"
            )
    else:
        lines.append(
            f"{by:36} {'load':>9} {'share':>6} {'interact':>9} {'headless':>9} {'tokens':>9} {'output':>8} {'calls':>6} {'sess':>5}"
        )
        for b in rows:
            share = 100 * b.load / total_load if total_load else 0.0
            lines.append(
                f"{b.name[:36]:36} {b.load:9.2f} {share:5.1f}% {b.kinds['interactive']:9.2f} {b.kinds['headless']:9.2f} "
                f"{_fmt_m(transcripts.total_tokens(b.tokens))} {b.tokens['output'] / 1e3:7.0f}k {b.calls:6d} {len(b.sessions):5d}"
            )
    hidden = len(result["rows"]) - len(rows)
    if hidden > 0:
        rest = sum(b.load for b in result["rows"][top:])
        lines.append(f"... {hidden} more row(s), load {rest:,.2f}")
    lines.append("")
    lines.append(
        "load = tokens weighted per model by budgeter/config.json model_weights "
        "(API list-price ratios, relative only; not a bill)."
    )
    if result["unweighted_models"]:
        lines.append(
            "unweighted models (counted as 0 load, add them to model_weights): "
            + ", ".join(result["unweighted_models"])
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = collect(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        payload = dict(result)
        payload["rows"] = [b.as_dict() for b in result["rows"][: args.top]]
        print(json.dumps(payload, indent=2))
    else:
        print(render(result, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
