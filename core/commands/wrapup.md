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

Review the **entire session** for non-obvious discoveries, workarounds, deferred work, and untracked bugs. Write any missing learnings or TODOs first, then generate a handoff note:

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
