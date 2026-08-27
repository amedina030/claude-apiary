#!/usr/bin/env python3
"""Compass backfill — extract observations from historical session transcripts.

For each selected session, extracts the user/assistant conversation, asks
headless Claude to identify 3-7 personality observations, validates the
returned JSON, and writes it as
``compass/observations/<session_id_short>.json``.

Selection (combinable):
    --last N              N most recent transcripts by mtime
    --session-ids LIST    Comma-separated 8-char prefixes or full UUIDs
    --since YYYY-MM-DD    Only transcripts modified on/after this date

If no selectors are given, exits 1 to avoid accidentally backfilling
the entire history.

Usage:
    backfill.py --last 3
    backfill.py --session-ids 1089da5c,8123e697
    backfill.py --since 2026-04-10
    backfill.py --last 5 --since 2026-04-01     # intersection

Exit codes:
    0 — at least one observation file written
    1 — no selectors / no matching sessions / nothing written
    2 — claude subprocess failed for every selected session
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compass import store
from core.hooks.extract_transcript import extract_conversation, read_session_jsonl
from core.utils.project import get_project_key, project_key_from_path
from core.utils.timeutil import ISO_FORMAT, now_iso
from runner.claude_subprocess import run_claude

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Hard cap on prompt size we send to claude. Long sessions get tail-truncated.
MAX_PROMPT_CHARS = 200_000
MAX_MSG_CHARS = 4_000  # truncate any single message above this length


def _candidate_transcripts_dirs() -> list[Path]:
    """Return possible project transcript dirs, in preference order.

    Claude Code's CLI stores transcripts under a cwd-derived key
    (e.g. ``D--Professional-claude-apiary``), but the rest of apiary
    prefers the stable key from ``.claude-project-key``. Both are valid
    candidates depending on which one actually contains .jsonl files
    on this machine.
    """
    cwd = Path.cwd()
    stable = get_project_key(cwd)
    legacy = project_key_from_path(cwd)
    seen: set[str] = set()
    out: list[Path] = []
    for key in (legacy, stable):  # legacy first — that's where Claude CLI writes
        if key in seen:
            continue
        seen.add(key)
        out.append(CLAUDE_PROJECTS_DIR / key)
    return out


def _list_transcripts() -> list[Path]:
    for root in _candidate_transcripts_dirs():
        if not root.is_dir():
            continue
        files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files
    return []


def _match_session_id(path: Path, ids: set[str]) -> bool:
    stem = path.stem.lower()
    short = stem[:8]
    for needle in ids:
        n = needle.lower()
        if n == short or n == stem:
            return True
    return False


def _select_transcripts(args: argparse.Namespace) -> list[Path]:
    transcripts = _list_transcripts()
    if not transcripts:
        return []

    selected = transcripts

    if args.session_ids:
        ids = {s.strip() for s in args.session_ids.split(",") if s.strip()}
        selected = [p for p in selected if _match_session_id(p, ids)]

    if args.since:
        try:
            cutoff = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"--since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            sys.exit(1)
        cutoff_ts = cutoff.timestamp()
        selected = [p for p in selected if p.stat().st_mtime >= cutoff_ts]

    if args.last is not None:
        selected = selected[: args.last]

    return selected


def _captured_at(transcript_path: Path) -> str:
    """When the session happened — the transcript's mtime, never ``now()``.

    ``synthesize.py`` sorts observations newest-first by ``captured_at``,
    tells the model to prefer the more recent one on conflict, and counts
    only the last few sessions toward volatile dimensions. Stamping the
    backfill time made every historical session look like it happened at
    the moment of the backfill, which inverted all three (review knowledge
    Bug 5). The transcript's last write is the closest thing on disk to
    the end of the session it records.
    """
    try:
        mtime = transcript_path.stat().st_mtime
    except OSError:
        # Unreadable stat is not worth failing the session over; now() is
        # the same (wrong but harmless) answer this used to give always.
        return now_iso()
    return datetime.fromtimestamp(mtime, timezone.utc).strftime(ISO_FORMAT)


def _truncate_messages(conversation: list[dict]) -> list[dict]:
    out: list[dict] = []
    for msg in conversation:
        text = msg.get("text", "")
        if len(text) > MAX_MSG_CHARS:
            text = text[:MAX_MSG_CHARS] + "…[truncated]"
        out.append({"role": msg["role"], "text": text})
    return out


def _build_prompt(
    transcript_messages: list[dict], dimensions: dict, session_id: str, captured_at: str
) -> str:
    dim_block = "\n".join(
        f"- **{d['name']}** ({'volatile' if d.get('volatile') else 'stable'}): {d['description']}"
        for d in dimensions["dimensions"]
    )
    valid_dim_names = [d["name"] for d in dimensions["dimensions"]]
    transcript_json = json.dumps(transcript_messages, ensure_ascii=False)

    return f"""You are the compass observer. Read a single Claude Code session transcript and extract 3–7 personality observations about the user.

## Lane

Compass captures **personality and behavior** — *how* the user engages, not *what* they know. Facts, rules, and explicit preferences belong elsewhere. If something reads more like a fact ("user uses Python") than a behavior pattern, omit it.

## Dimensions

Each observation must tag exactly one dimension from this list:

{dim_block}

Volatile dimensions reflect *current* state, not stable personality. Mark mood/tone observations as `"volatility": "volatile"`. Mark stable personality traits as `"volatility": "stable"`.

## Quality bar

- Each observation must be grounded in a specific moment in the transcript. Cite an evidence quote or short paraphrase.
- Skip dimensions where the session showed no clear signal. Do NOT pad to hit 3-7 if the signal isn't there. Returning fewer observations is better than fabricating.
- If nothing is clear, return `"observations": []`.

## Output format

Return a single JSON object. No preamble, no commentary, no code fences. Schema:

{{
  "session_id": "{session_id}",
  "captured_at": "{captured_at}",
  "observations": [
    {{
      "dimension": "<one of: {", ".join(valid_dim_names)}>",
      "observation": "<1-2 sentences describing the trait/pattern>",
      "evidence": "<short quote or paraphrase from the transcript>",
      "volatility": "stable" | "volatile"
    }}
  ]
}}

## Transcript

{transcript_json}
"""


def _extract_json(stdout: str) -> dict | None:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(envelope, dict) and "result" in envelope:
        text = str(envelope["result"]).strip()
    else:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _process_one(transcript_path: Path, dimensions: dict, *, model: str | None, force: bool) -> str:
    """Process a single transcript. Returns 'wrote' | 'skipped' | 'failed'."""
    sid_full = transcript_path.stem
    sid_short = sid_full[:8].lower()

    out_path = store.observation_path(sid_short)
    if out_path.exists() and not force:
        print(f"  skip {sid_short}: observation already exists (use --force to overwrite)")
        return "skipped"

    entries = read_session_jsonl(str(transcript_path))
    conversation = extract_conversation(entries)
    if len(conversation) < 4:
        print(f"  skip {sid_short}: conversation too short ({len(conversation)} messages)")
        return "skipped"

    conversation = _truncate_messages(conversation)
    captured_at = _captured_at(transcript_path)

    prompt = _build_prompt(conversation, dimensions, sid_short, captured_at)
    if len(prompt) > MAX_PROMPT_CHARS:
        # Drop oldest messages until under cap.
        while len(prompt) > MAX_PROMPT_CHARS and len(conversation) > 4:
            conversation = conversation[1:]
            prompt = _build_prompt(conversation, dimensions, sid_short, captured_at)

    rc, stdout, stderr = run_claude(prompt, model=model)
    if rc != 0:
        print(f"  fail {sid_short}: claude rc={rc} {stderr[:120]}", file=sys.stderr)
        return "failed"

    payload = _extract_json(stdout)
    if payload is None:
        print(f"  fail {sid_short}: could not parse JSON from claude output", file=sys.stderr)
        return "failed"

    # Don't trust claude to echo back the session_id correctly — it can
    # latch onto commit hashes or other hex strings in the transcript.
    # Authoritative session_id is the transcript filename.
    payload["session_id"] = sid_short
    payload["captured_at"] = captured_at

    errors = store.validate_observation(payload)
    if errors:
        print(f"  fail {sid_short}: validation: {'; '.join(errors[:3])}", file=sys.stderr)
        return "failed"

    store.ensure_layout()
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    n = len(payload.get("observations", []))
    print(f"  wrote {sid_short}: {n} observation(s)")
    return "wrote"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill compass observations from session transcripts"
    )
    parser.add_argument(
        "--last", type=int, default=None, help="N most recent transcripts (by mtime)"
    )
    parser.add_argument(
        "--session-ids", default=None, help="Comma-separated session prefixes or UUIDs"
    )
    parser.add_argument(
        "--since", default=None, help="Only transcripts modified on/after YYYY-MM-DD"
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing observation files")
    parser.add_argument("--model", default=None, help="Override claude model")
    args = parser.parse_args()

    if args.last is None and not args.session_ids and not args.since:
        print("at least one selector is required: --last, --session-ids, --since", file=sys.stderr)
        return 1

    selected = _select_transcripts(args)
    if not selected:
        print("no matching transcripts", file=sys.stderr)
        return 1

    dimensions = store.load_dimensions()

    print(f"processing {len(selected)} transcript(s)")
    counts = {"wrote": 0, "skipped": 0, "failed": 0}
    for path in selected:
        outcome = _process_one(path, dimensions, model=args.model, force=args.force)
        counts[outcome] += 1

    print(f"\nwrote={counts['wrote']} skipped={counts['skipped']} failed={counts['failed']}")
    if counts["wrote"] > 0:
        return 0
    if counts["failed"] > 0 and counts["skipped"] == 0:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
