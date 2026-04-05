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
python <repo_dir>/scribe/notes.py add --type handoff --session-id <session_id_8char> --content "<handoff>"
```

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
