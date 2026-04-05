---
name: backfill-handoffs
description: Process unseen session transcripts into handoff notes
user-invocable: true
---

Process unseen session transcripts listed in the `[startup]` context block. Only run this if the `[startup]` context contains `unseen_sessions` that are NOT "none".

## What to do

Spawn an agent (subagent_type: "general-purpose", run_in_background: true, model: "haiku") with the following prompt. Replace `<repo_dir>` with the current repo working directory, and `<session_id>` with the current session ID from the `[session]` context (first 8 chars). If `[session]` context is not available, check `[budgeter]` context as a fallback.

---

**Agent prompt:**

You are a transcript processing agent. Your job is to generate handoffs and extract missed learnings/TODOs from unseen session transcripts.

### Step 1: Process unseen sessions

For each unseen session from the `[startup]` context:
1. Run `python <repo_dir>/core/hooks/extract_transcript.py <transcript_path>` to extract clean messages.
2. If output is empty, skip this session.
3. **Important:** Read the ENTIRE transcript before classifying. Early messages are often startup boilerplate — the real work typically starts midway through. Do NOT classify a session as "startup-only" unless it truly contains nothing beyond startup initialization and brief status checks.
4. Produce a handoff with these sections:
   ```
   ## Session <prev-id> Handoff
   **Role:** <role> | **Mission:** <mission>
   ### What was done
   ### Key decisions
   ### What's pending
   ### Where it stopped
   ```
   Be concise but specific — file names, function names, concrete details.
5. Save the handoff:
   ```bash
   python <repo_dir>/scribe/notes.py add --type handoff --session-id <prev-id> --auto --if-no-handoff-for <prev-id> --content "<handoff>"
   ```
6. **Extract missed learnings and TODOs.** While reading the transcript, look for:
   - Non-obvious workarounds or fixes that were discovered but not saved as learnings
   - Work that was deferred or left incomplete that wasn't captured as a TODO
   - Bugs observed but not tracked
   
   For each one found, save it:
   ```bash
   python <repo_dir>/scribe/notes.py learn --content "<what was learned>" --session-id <prev-id>
   python <repo_dir>/scribe/notes.py add --type todo --content "<what was deferred>" --session-id <prev-id> --auto
   ```
   Only add items that are genuinely missing — check existing notes/learnings first to avoid duplicates.

### Step 2: Return summary

Compose and return a message with EXACTLY this structure (no extras):

```
**Handoffs generated:** <count> — <list of session IDs, or "None">
**Extracted from transcripts:** <count> learnings, <count> TODOs — or "None"
```

Keep the entire output under 200 words.

---

## After launching the agent

Do NOT wait for the agent to finish. Immediately continue with the user's request. When the agent completes in the background, output its summary prefixed with "Transcripts processed:" so the user knows handoffs are ready.

## Cost logging

When the transcript agent completes, the task notification includes a `<usage>` block. Pipe the **raw usage block** (copy-paste the entire `<usage>...</usage>` tag) to the logging script via stdin:

```bash
echo '<usage>...</usage>' | python <repo_dir>/budgeter/log_agent_cost.py --session-id "<full_session_id>" --agent "backfill-handoffs" --cwd "<repo_dir>"
```

Use the full session UUID (not the 8-char prefix) so it matches other budgeter entries. Run this silently — do not mention the cost logging to the user.
