---
name: wrapup
description: Commit, push, and generate a session handoff note
user-invocable: true
---

End-of-session wrap-up. Perform these three steps in order:

## Step 1: Commit

Follow the standard git commit flow from your system instructions (git status, git diff, git log, then stage and commit). If there are no changes to commit, skip to Step 2.

## Step 2: Push

Push the current branch to the remote. If the branch has no upstream, use `git push -u origin <branch>`.

## Step 3: Handoff

Review the **entire session** for deferred work and untracked bugs. File any missing TODOs first, then generate a handoff note:

```bash
python ~/.claude/apiary_launch.py scribe/notes.py add --type handoff --session-id <session_id_8char> --summary "<one-line abstract>" --content "<handoff>"
```

The handoff is written as an individual .md file under `.apiary/scribe/handoffs/` and indexed in `handoffs/index.jsonl`. The `--summary` argument is **required** for handoffs and must be a single concrete sentence (≤300 chars) — this is what every future session sees in startup context, so name the file/area touched and the outcome (e.g. `"Session abc12345: fixed scribe v2 handoff de-dup in core/startup.py + notes.py guard, all tests pass, commit 9a32226"`). Do not just restate "session X handoff" — that adds no information.

The handoff must follow this structure:

```
## Session <session_id_8char> Handoff
**Role:** <role> | **Mission:** <mission>

### What was done
### Key decisions
### What's pending
### Where it stopped
```

Be concise but specific — file names, function names, concrete details.

## Step 4: Compass capture (non-blocking)

Extract personality/behavior observations from this session and write them to `<repo>/.apiary/compass/observations/<session_id_8char>.json`. These accumulate into a personality profile (`personality.md`) that future sessions read at startup so Claude can anticipate the user's preferences.

### What to capture

Compass is about **how** the user engages — personality, behavior, quirks — *not* what they know or rules they've stated. Facts and explicit preferences belong in auto-memory, not here.

Look for signals across these dimensions (load full descriptions from the dimensions config if needed):

- `communication_style`, `decision_making`, `pushback`, `engagement`, `autonomy`, `risk_tolerance`, `trust_calibration`, `meta_awareness`, `mood_tone`

`mood_tone` is **volatile** (current state, not stable trait); the rest are stable.

Full dimension descriptions:
```bash
cat "$(python ~/.claude/apiary_launch.py --print-repo-path)/compass/dimensions.json"
```

### Quality bar

- 3–7 observations across the dimensions where you saw clear signal. Skip dimensions with no signal — do NOT pad.
- Each observation needs an evidence quote/paraphrase from the actual session.
- If no clear signal at all, write `"observations": []` — empty is honest, fabricated is harmful.
- Tag mood/tone observations as `"volatility": "volatile"`. Everything else is `"stable"`.

### Schema

Write this exact shape to `<repo>/.apiary/compass/observations/<session_id_8char>.json`:

```json
{
  "session_id": "<8-char prefix>",
  "captured_at": "<ISO 8601 UTC, e.g. 2026-04-17T20:30:00Z>",
  "observations": [
    {
      "dimension": "<one of the dimension names>",
      "observation": "<1–2 sentences describing the trait/pattern>",
      "evidence": "<short quote or paraphrase from this session>",
      "volatility": "stable"
    }
  ]
}
```

### Validation

After writing, validate the file:

```bash
python ~/.claude/apiary_launch.py compass/observations.py validate "$(python ~/.claude/apiary_launch.py --print-repo-path)/.apiary/compass/observations/<sid>.json"
```

If validation fails, fix and re-write. If validation keeps failing, log a brief warning to the user and move on — capture is non-blocking and should never prevent /wrapup from completing.

### Skip conditions

- Session was startup-only or trivial (< ~5 user messages of real interaction): write nothing, skip silently.
- Compass directory write fails: log warning, skip silently. Do NOT block /wrapup.
