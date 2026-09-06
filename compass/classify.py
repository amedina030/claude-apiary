#!/usr/bin/env python3
"""Classify a session's turn pairs into rule events — the model step of D-2026-62.

Reads ``<state-dir>/compass/turns/<sid>.jsonl`` (written by the Stop hook
``core/hooks/compass_pair_log.py``), sends every pair in **one** batched
``claude -p`` call (Sonnet by default, prompt via stdin per L-2026-18) with the
fixed vocabulary below, validates the reply against that vocabulary, writes
``<state-dir>/compass/events/<sid>.json`` and regenerates ``rules.md``.

Vocabulary (fixed; anything outside it is dropped, never guessed):

* ``type``     — ``correction`` (Claude did A, the user redirected to B; includes
  an interrupt followed by a redirect and a transparency-miss question),
  ``acceptance`` (Claude did A, the user said go), ``anticipation_miss`` (the
  user asked what the previous reply should have pre-empted).
* ``section``  — ``judgment`` | ``output`` | ``anticipation``.
* ``rule``     — an id from the current rule table, or ``null``.
* ``polarity`` — ``confirm`` when the event supports the rule as a description
  of what the user wants (an acceptance of rule-conformant behaviour, or a
  correction *towards* it); ``contradict`` when the user steered away from what
  the rule prescribes.
* ``action``   — a short phrase naming what Claude did or should have done.
* ``quote``    — the user's words, verbatim, at most a sentence.

The classifier reads the PAIR, never the user turn alone, and an empty event
list is the expected output for most pairs.

Usage::

    classify.py <sid> [--dry-run] [--model sonnet] [--min-pairs 5] [--force] [--no-build]
    classify.py --catch-up [--min-age-hours 2] [--limit N] [--dry-run]

Exit codes: 0 classified (or honestly skipped), 1 model or validation failure
(nothing written), 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compass import rules, store, turns  # noqa: E402
from core.utils.atomic import write_json_atomic  # noqa: E402

DEFAULT_MODEL = "sonnet"
DEFAULT_MIN_PAIRS = 5  # matches /wrapup's "< ~5 user messages: skip" rule
DEFAULT_CATCHUP_MIN_AGE_HOURS = 2.0  # a turns file this fresh may be a live session
MAX_PAIRS_PER_CALL = 120
ACTION_MAX_CHARS = 200
QUOTE_MAX_CHARS = 300
CLAUDE_TIMEOUT_SECONDS = 600


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def _rule_lines(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        kind = "principle" if row.get("kind") == "principle" else f"specific of {row.get('parent')}"
        lines.append(f"- {row['id']} [{row['section']}; {kind}] {row['rule'].strip()}")
    return "\n".join(lines)


def build_prompt(pairs: list[dict], rows: list[dict]) -> str:
    """The batched classification prompt for one session."""
    numbered = []
    for i, pair in enumerate(pairs):
        numbered.append(
            f"### pair {i}\n"
            f"ASSISTANT (what Claude had just said):\n{pair.get('assistant', '').strip()}\n\n"
            f"USER (the reply):\n{pair.get('user', '').strip()}\n"
        )
    return (
        "You are classifying (assistant, user) turn pairs from one Claude Code session "
        "between an AI assistant and its user. The goal is to learn the user's standing "
        "preferences from what they corrected and what they accepted, so future sessions "
        "can act on them without asking.\n\n"
        "Read each PAIR as a unit: the assistant text is what Claude had just said or done; "
        "the user text is the reply. Never classify the user turn alone.\n\n"
        "Emit an event only when the reply clearly is one of:\n"
        "- correction: Claude did A, the user redirected to B (includes an interrupt followed "
        "by a redirect, and a transparency-miss question such as 'wait, what is running?').\n"
        "- acceptance: Claude proposed or did A and the user said go ('yep', 'next', 'your rec', "
        "'approved', 'do it').\n"
        "- anticipation_miss: the user asked something the previous reply should have "
        "pre-empted ('why not X', 'how do I check', 'is this it?', 'what does this cost').\n\n"
        "Ordinary task instructions, questions about the codebase, and small talk are NOT events. "
        "An empty list is the expected answer for most pairs. Do not pad.\n\n"
        "For each event give:\n"
        "- pair: the pair number\n"
        "- type: correction | acceptance | anticipation_miss\n"
        "- section: judgment (what to decide) | output (how to write) | anticipation (what to pre-empt)\n"
        "- rule: the id of the rule below that the event bears on, or null if none fits\n"
        "- polarity: confirm if the event supports the rule as a description of what this user "
        "wants (acceptance of rule-conformant behaviour, or a correction TOWARDS the rule); "
        "contradict if the user steered AWAY from what the rule prescribes\n"
        "- action: a short phrase naming what Claude did or should have done (under 20 words)\n"
        "- quote: the user's words, verbatim, at most one sentence\n\n"
        "Current rule table (second person, addressed to Claude):\n"
        f"{_rule_lines(rows)}\n\n"
        "Respond with a JSON object only, no prose and no markdown fence:\n"
        '{"events": [{"pair": 0, "type": "...", "section": "...", "rule": "J1", '
        '"polarity": "confirm", "action": "...", "quote": "..."}]}\n\n'
        f"{len(pairs)} pairs follow.\n\n" + "\n".join(numbered)
    )


# ---------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------


def extract_json(stdout: str) -> dict | None:
    """The JSON object in claude's ``--output-format json`` envelope (or raw)."""
    text = stdout.strip()
    if not text:
        return None
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict) and "events" in envelope:
        return envelope
    if isinstance(envelope, dict) and "result" in envelope:
        text = str(envelope["result"]).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:-1] if lines and lines[-1].strip().startswith("```") else lines[1:]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def validate_events(
    payload: dict, pairs: list[dict], rows: list[dict]
) -> tuple[list[dict], list[str]]:
    """Keep events that fit the vocabulary; report each drop. Never guesses."""
    section_of = {str(r["id"]): r.get("section") for r in rows}
    events: list[dict] = []
    errors: list[str] = []
    raw = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return [], ["payload has no 'events' list"]
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"events[{i}] is not an object")
            continue
        pair_index = item.get("pair")
        if not isinstance(pair_index, int) or not 0 <= pair_index < len(pairs):
            errors.append(f"events[{i}].pair {pair_index!r} out of range")
            continue
        etype = item.get("type")
        if etype not in rules.EVENT_TYPES:
            errors.append(f"events[{i}].type {etype!r} not in {list(rules.EVENT_TYPES)}")
            continue
        polarity = item.get("polarity")
        if polarity not in rules.POLARITIES:
            errors.append(f"events[{i}].polarity {polarity!r} not in {list(rules.POLARITIES)}")
            continue
        rule = item.get("rule")
        if rule is not None:
            rule = str(rule).strip().upper()
            if rule not in section_of:
                errors.append(f"events[{i}].rule {rule!r} is not a known rule id")
                continue
        section = item.get("section")
        if rule is not None:
            section = section_of[rule]
        if section not in rules.SECTIONS:
            errors.append(f"events[{i}].section {section!r} not in {list(rules.SECTIONS)}")
            continue
        action = str(item.get("action") or "").strip()
        if not action:
            errors.append(f"events[{i}].action is empty")
            continue
        quote = " ".join(str(item.get("quote") or "").split())
        pair = pairs[pair_index]
        events.append(
            {
                "pair": pair_index,
                "prompt_id": pair.get("prompt_id"),
                "ts": pair.get("ts"),
                "type": etype,
                "section": section,
                "rule": rule,
                "polarity": polarity,
                "action": action[:ACTION_MAX_CHARS],
                "quote": quote[:QUOTE_MAX_CHARS],
            }
        )
    return events, errors


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_events(
    sid: str,
    *,
    pairs: int,
    events: list[dict],
    model: str | None,
    dropped: int = 0,
    skipped: str | None = None,
) -> Path:
    target = store.events_path(sid)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        target,
        {
            "session_id": store.short_session_id(sid),
            "classified_at": _now_iso(),
            "model": model,
            "pairs": pairs,
            "events": events,
            "dropped": dropped,
            "skipped": skipped,
        },
        indent=2,
        trailing_newline=True,
    )
    return target


def _rebuild_rules() -> str:
    result = rules.build()
    target = store.rules_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    from core.utils.atomic import write_text_atomic

    write_text_atomic(target, result["text"])
    return f"rules.md: {len(result['rows'])} rows, {result['events']} events, " + (
        f"{len(result['flagged'])} flagged, {len(result['proposed'])} proposed"
    )


def classify_session(
    sid: str,
    *,
    model: str = DEFAULT_MODEL,
    min_pairs: int = DEFAULT_MIN_PAIRS,
    dry_run: bool = False,
    force: bool = False,
    rebuild: bool = True,
    run_claude=None,
) -> int:
    """Classify one session. Returns the process exit code."""
    short = store.short_session_id(sid)
    pairs = turns.load_pairs(sid)
    if not pairs:
        print(f"{short}: no turns file or no pairs; nothing to classify", file=sys.stderr)
        return 0

    existing = store.events_path(sid)
    if existing.is_file() and not force and not dry_run:
        try:
            prior = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior = {}
        if isinstance(prior, dict) and prior.get("pairs") == len(pairs):
            print(f"{short}: already classified ({len(pairs)} pairs); pass --force to redo")
            return 0

    considered = pairs[-MAX_PAIRS_PER_CALL:]
    offset = len(pairs) - len(considered)
    rows = rules.merge_rows(
        list(store.load_seed_rules().get("rules", [])),
        rules.load_manual_rows(),
        datetime.now(timezone.utc),
    )
    prompt = build_prompt(considered, rows)

    if dry_run:
        sys.stdout.write(prompt)
        return 0

    if len(pairs) < min_pairs:
        target = _write_events(
            sid, pairs=len(pairs), events=[], model=None, skipped="too_few_pairs"
        )
        print(f"{short}: {len(pairs)} pair(s) < {min_pairs}; recorded as skipped at {target}")
        if rebuild:
            print(_rebuild_rules())
        return 0

    if run_claude is None:
        from runner.claude_subprocess import run_claude as _run

        run_claude = _run
    started = time.time()
    rc, stdout, stderr = run_claude(
        prompt,
        model=model,
        timeout=CLAUDE_TIMEOUT_SECONDS,
        max_turns=1,
        allowed_tools=(),
        disallowed_tools=(),
        permission_mode=None,
    )
    if rc != 0:
        print(
            f"{short}: claude subprocess failed (rc={rc}): {stderr.strip()[:500]}", file=sys.stderr
        )
        return 1
    payload = extract_json(stdout)
    if payload is None:
        print(f"{short}: claude returned no JSON object; nothing written", file=sys.stderr)
        return 1
    events, errors = validate_events(payload, considered, rows)
    for event in events:
        event["pair"] += offset
    for err in errors:
        print(f"{short}: dropped event: {err}", file=sys.stderr)
    target = _write_events(sid, pairs=len(pairs), events=events, model=model, dropped=len(errors))
    print(
        f"{short}: {len(events)} event(s) from {len(considered)} pair(s) in "
        f"{time.time() - started:.0f}s -> {target}"
    )
    if rebuild:
        print(_rebuild_rules())
    return 0


def pending_sessions(
    *, min_age_hours: float = DEFAULT_CATCHUP_MIN_AGE_HOURS, now=None
) -> list[str]:
    """Turn files with no up-to-date events file, old enough to be finished."""
    now = time.time() if now is None else now
    pending: list[str] = []
    for sid in turns.list_turn_sessions():
        turns_file = store.turns_path(sid)
        try:
            age_hours = (now - turns_file.stat().st_mtime) / 3600
        except OSError:
            continue
        if age_hours < min_age_hours:
            continue
        events_file = store.events_path(sid)
        if events_file.is_file():
            try:
                prior = json.loads(events_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prior = {}
            if isinstance(prior, dict) and prior.get("pairs") == len(turns.load_pairs(sid)):
                continue
        pending.append(sid)
    return pending


def cmd_catch_up(args: argparse.Namespace) -> int:
    pending = pending_sessions(min_age_hours=args.min_age_hours)
    if args.limit is not None:
        pending = pending[: args.limit]
    if not pending:
        print("catch-up: nothing pending")
        return 0
    print(f"catch-up: {len(pending)} session(s) pending")
    worst = 0
    for i, sid in enumerate(pending):
        rc = classify_session(
            sid,
            model=args.model,
            min_pairs=args.min_pairs,
            dry_run=args.dry_run,
            force=True,
            rebuild=(i == len(pending) - 1) and not args.no_build,
        )
        worst = max(worst, rc)
    return worst


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify a session's turn pairs into compass rule events (one Sonnet call)"
    )
    parser.add_argument("session_id", nargs="?", help="Session id (8-char prefix or full UUID)")
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Classify every finished session with no events file",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"claude model alias (default {DEFAULT_MODEL!r})"
    )
    parser.add_argument(
        "--min-pairs",
        type=int,
        default=DEFAULT_MIN_PAIRS,
        help=f"Below this many pairs the session is recorded as skipped, no model call (default {DEFAULT_MIN_PAIRS})",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=DEFAULT_CATCHUP_MIN_AGE_HOURS,
        help=f"--catch-up only: leave turns files younger than this alone (default {DEFAULT_CATCHUP_MIN_AGE_HOURS:g})",
    )
    parser.add_argument("--limit", type=int, help="--catch-up only: classify at most N sessions")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the prompt; call nothing, write nothing"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-classify even if an events file is current"
    )
    parser.add_argument(
        "--no-build", action="store_true", help="Do not regenerate rules.md afterwards"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.catch_up:
        return cmd_catch_up(args)
    if not args.session_id:
        parser.error("a session id or --catch-up is required")
    return classify_session(
        args.session_id,
        model=args.model,
        min_pairs=args.min_pairs,
        dry_run=args.dry_run,
        force=args.force,
        rebuild=not args.no_build,
    )


if __name__ == "__main__":
    sys.exit(main())
