#!/usr/bin/env python3
"""Compass synthesizer — builds personality.md from active observations.

Reads:
  - compass/observations/<sid>.json      (active per-session observations)
  - compass/personality.md               (previous synthesis, for continuity)
  - compass/corrections.md               (manual high-weight evidence)
  - compass/dimensions.json              (dimension config)

Writes:
  - compass/personality.md               (new synthesis)

Invokes a headless ``claude -p`` subprocess via runner.claude_subprocess.
Used by the ``/compass-sync`` slash command and the weekly cron entrypoint.

Usage:
    synthesize.py [--dry-run] [--model MODEL]

Exit codes:
  0 — wrote personality.md
  1 — no active observations to synthesize from (no-op)
  2 — claude subprocess failed; previous personality.md untouched
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compass import store
from runner.claude_subprocess import run_claude


VOLATILE_RECENT_WINDOW = 5  # last N sessions count toward volatile dimensions
CRON_MIN_AGE_DAYS = 7        # --cron mode no-ops if personality.md is younger than this


def _load_active(now_active: list[Path]) -> list[dict]:
    """Load + sort observations newest-first by captured_at."""
    loaded: list[tuple[str, dict]] = []
    for path in now_active:
        data = store.load_observation(path)
        if data is None:
            continue
        loaded.append((data.get("captured_at", ""), data))
    loaded.sort(key=lambda item: item[0], reverse=True)
    return [data for _, data in loaded]


def _build_prompt(observations: list[dict], previous_personality: str,
                  corrections: str, dimensions: dict) -> str:
    """Compose the synthesis prompt for headless Claude."""
    volatile_names = [d["name"] for d in dimensions["dimensions"] if d.get("volatile")]
    dim_block = "\n".join(
        f"- **{d['name']}** ({'volatile' if d.get('volatile') else 'stable'}): {d['description']}"
        for d in dimensions["dimensions"]
    )
    obs_block = json.dumps(observations, indent=2, ensure_ascii=False)

    return f"""You are the compass synthesizer for a personal Claude Code workflow. Your job is to read raw per-session behavioral observations of one user and produce a single markdown profile (`personality.md`) that future Claude sessions will read at startup to inform how they interact with this user.

## Lane

Compass captures **personality and behavior** — *how* the user engages, not *what* they know. Facts, rules, and explicit preferences live in a separate auto-memory system. Do not duplicate that material here. If an observation reads more like a fact or a rule than a behavior pattern, omit it.

## Dimensions to cover

{dim_block}

Volatile dimensions ({', '.join(volatile_names) or '(none)'}) reflect *current* state, not stable personality. For volatile dimensions, only consider the most recent {VOLATILE_RECENT_WINDOW} sessions' observations. For stable dimensions, integrate across the full active window.

## Conflict handling

When observations disagree, prefer the more recent one (compare `captured_at`). Note evolution explicitly when a clear shift has occurred (e.g. "Earlier sessions showed X; recent sessions show Y").

## Continuity

The previous personality.md is provided. Treat it as your prior understanding. The new synthesis should *evolve* the profile — preserve traits still supported by recent observations, update traits where new evidence contradicts, and drop traits that have no recent supporting evidence (older than the active window).

## Corrections

The corrections.md file contains the user's own annotations on the profile (manual high-weight evidence). When corrections conflict with raw observations, the corrections win.

## Output format

Output only the markdown content of the new `personality.md`. No preamble, no commentary, no code fences. Structure:

```
# Personality profile

_Synthesized from N sessions, last updated YYYY-MM-DD._

## <dimension>
<2-4 sentences of prose describing this dimension as observed. Cite supporting evidence inline where it adds clarity, e.g. "consistent across N sessions" or "noted in recent sessions".>

## <next dimension>
...
```

Only include sections for dimensions that have meaningful observation support. Skip dimensions with no signal — do not pad.

Aim for 400-800 words total. Concise prose beats exhaustive lists.

---

## Previous personality.md

{previous_personality or '(none — first synthesis)'}

---

## Corrections

{corrections or '(none)'}

---

## Active observations ({len(observations)} session(s), newest first)

{obs_block}
"""


def cmd_synthesize(args: argparse.Namespace) -> int:
    store.ensure_layout()

    if args.cron:
        p = store.personality_path()
        if p.exists():
            age_days = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 86400
            if age_days < CRON_MIN_AGE_DAYS:
                print(f"--cron: personality.md is {age_days:.1f} days old "
                      f"(< {CRON_MIN_AGE_DAYS}), skipping")
                return 0

    active_paths = store.list_active_observations()
    if not active_paths:
        print("no active observations; nothing to synthesize", file=sys.stderr)
        return 1

    observations = _load_active(active_paths)
    if not observations:
        print("active observation files exist but all failed validation; aborting",
              file=sys.stderr)
        return 1

    previous = ""
    if store.personality_path().exists():
        previous = store.personality_path().read_text(encoding="utf-8")

    corrections = ""
    if store.corrections_path().exists():
        corrections = store.corrections_path().read_text(encoding="utf-8")

    dimensions = store.load_dimensions()
    prompt = _build_prompt(observations, previous, corrections, dimensions)

    if args.dry_run:
        print(prompt)
        return 0

    rc, stdout, stderr = run_claude(prompt, model=args.model)
    if rc != 0:
        print(f"claude subprocess failed (rc={rc}): {stderr}", file=sys.stderr)
        return 2

    text = _extract_markdown(stdout)
    if not text.strip():
        print("claude returned empty output; previous personality.md untouched",
              file=sys.stderr)
        return 2

    store.personality_path().write_text(text, encoding="utf-8")
    print(f"wrote {store.personality_path()} ({len(text)} chars from "
          f"{len(observations)} session(s))")
    return 0


def _extract_markdown(stdout: str) -> str:
    """Pull the assistant text out of claude's --output-format json envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()
    if isinstance(envelope, dict) and "result" in envelope:
        text = str(envelope["result"]).strip()
    else:
        text = stdout.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthesize personality.md from observations")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt instead of calling claude")
    parser.add_argument("--model", default=None,
                        help="override claude model (default: CLI default)")
    parser.add_argument("--cron", action="store_true",
                        help=f"weekly-throttle mode: no-op if personality.md is "
                             f"younger than {CRON_MIN_AGE_DAYS} days")
    args = parser.parse_args()
    return cmd_synthesize(args)


if __name__ == "__main__":
    sys.exit(main())
