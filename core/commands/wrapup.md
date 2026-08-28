---
name: wrapup
description: Commit, capture learnings + TODOs, and generate a session handoff note
user-invocable: true
---

End-of-session wrap-up. Perform these steps in order:

## Step 1: Commit

Follow the standard git commit flow from your system instructions (git status, git diff, git log, then stage and commit). If there are no changes to commit, skip to Step 2.

## Step 2: Capture learnings and TODOs

Review the **entire session** for non-obvious discoveries and deferred work, and write them down **before** the handoff — this is the primary knowledge-capture mechanism, so do not skip it.

**Learnings** — for each workaround, non-obvious project pattern, tool quirk, or better approach you discovered this session, write a learning:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py learn --content "<what you learned>" --session-id <session_id_8char>
```

Write a learning when you hit an error and found a workaround, found a better approach mid-task, a tool/API behaved unexpectedly and you figured out why, or you found a non-obvious project-specific pattern or constraint. Do **not** write one when the fix was obvious from the error message, it's general programming knowledge, it's already documented in the codebase/`docs/`/`CLAUDE.md`, or it duplicates an existing learning (update that one instead).

Pass `--tags` when you already know the right ones; otherwise leave the learning untagged. Wrapup must stay fast, so `learn` never calls a model on its own — `/review-learnings` runs `notes.py retrotag` to fill in the gaps in one batch.

**TODOs** — file any deferred or untracked work (including bugs found but not fixed) as todos:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type todo --session-id <session_id_8char> --content "<deferred work, with enough context to resume>"
```

## Step 3: Handoff

Generate a handoff note summarizing the session:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type handoff --session-id <session_id_8char> --summary "<one-line abstract>" --content "<handoff>"
```

The handoff is written as an individual .md file under `<state-dir>/scribe/handoffs/<year>/` and indexed in `handoffs/<year>/index.jsonl`. The `--summary` argument is **required** for handoffs and must be a single concrete sentence (≤300 chars) — this is what every future session sees in startup context, so name the file/area touched and the outcome (e.g. `"Session abc12345: fixed scribe v2 handoff de-dup in core/startup.py + notes.py guard, all tests pass, commit 9a32226"`). Do not just restate "session X handoff" — that adds no information.

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

Extract personality/behaviour observations from this session. `compass/capture.py` owns the schema, the target path and the validation — you only supply the observations.

Compass is about **how** the user engages — personality, behaviour, quirks — *not* what they know or rules they've stated. Facts and explicit preferences belong in auto-memory, not here.

**Skip silently** when the session was startup-only or trivial (< ~5 user messages of real interaction): write nothing, say nothing.

Otherwise:

1. Read the dimensions and which of them are volatile:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/capture.py dimensions
   ```

2. Write the observation JSON to a scratch file (`compass/capture.py template --session-id <sid>` prints the exact shape). Quality bar: 3–7 observations across the dimensions where you saw **clear signal**; skip the rest rather than padding. Every observation needs an evidence quote or paraphrase from this session. Tag mood/tone as `"volatility": "volatile"`, everything else `"stable"`. Honest emptiness is fine — `"observations": []` beats anything fabricated.

3. Store it:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/capture.py store --content-file <scratch.json> --session-id <session_id_8char>
   ```

`store` validates before it writes, so a rejected payload leaves nothing behind. Exit 0 means stored (or honestly empty and skipped). On a non-zero exit, fix the payload and run it once more; if it still fails, log a one-line warning to the user and move on. Capture is non-blocking and must never prevent `/wrapup` from completing.
