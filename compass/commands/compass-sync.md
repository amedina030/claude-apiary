---
name: compass-sync
description: Manually trigger compass synthesis — regenerate personality.md from active observations
user-invocable: true
---

Manually trigger the compass synthesizer. Normally this runs on a weekly cron, but invoke this skill for "I just had a big shift, sync now" cases.

## What this does

Reads active per-session observations from `<repo>/.apiary/compass/observations/`, plus the previous `personality.md` and `corrections.md`, and asks a headless `claude -p` subprocess to produce a new `personality.md`. The new file is read at every session startup via `/apiary-context`.

## Steps

### 1. Pre-check: are there any observations?

```bash
python ~/.claude/apiary_launch.py compass/observations.py count
```

If the count is `0`, tell the user there's nothing to synthesize yet and stop. They can populate observations via `/wrapup` (each session adds one) or `python ~/.claude/apiary_launch.py compass/backfill.py --last N` (historical).

### 2. Run the synthesizer

```bash
python ~/.claude/apiary_launch.py compass/synthesize.py
```

This calls `claude -p` headlessly. It typically takes 10–30 seconds. Exit codes:

- `0` — `personality.md` was rewritten. Report the new file location and char count to the user.
- `1` — no active observations (shouldn't happen if Step 1 passed; report and stop).
- `2` — claude subprocess failed; previous `personality.md` is untouched. Report the error to the user.

### 3. Show the user the new profile

After a successful sync, briefly preview what changed. Read the new file:

```bash
apiary_root=$(python ~/.claude/apiary_launch.py --print-repo-path)
```

Then `Read` `<apiary_root>/.apiary/compass/personality.md` and summarize the dimensions covered in 1-2 sentences. Don't dump the full file unless the user asks.

## Notes

- This command does NOT capture new observations — capture happens inline during `/wrapup`. `/compass-sync` only re-runs the synthesis step.
- If the user wants to influence synthesis without waiting for new observations to accumulate, they can edit `<repo>/.apiary/compass/corrections.md` and re-run this command. The synthesizer treats corrections as high-weight evidence.
