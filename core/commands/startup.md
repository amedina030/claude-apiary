---
name: startup
description: Session startup — declares identity, generates handoffs, loads notes
user-invocable: true
---

Launch a startup agent to handle session initialization. Do NOT execute these steps yourself — delegate them entirely to the agent.

## What to do

Spawn an agent (subagent_type: "general-purpose", run_in_background: true, model: "haiku") with the following prompt. Replace `<repo_dir>` with the current repo working directory, and `<session_id>` with the current session ID from the `[session]` context (first 8 chars). If `[session]` context is not available, check `[budgeter]` context as a fallback.

Also pass the **full text of the user's first message** to the agent as `<first_message>`.

---

**Agent prompt:**

You are a session startup agent. Your job is to initialize the session, process any unseen transcripts, and return a summary. Most of the work is done by `core/startup.py` — you only need LLM reasoning for transcript analysis.

### Step 1: Initialize session and detect unseen sessions

Run:
```bash
python <repo_dir>/core/startup.py init --session-id "<session_id>" --first-message "<first_message>" --repo-dir "<repo_dir>"
```

This returns JSON with `identity` and `unseen_sessions`. Parse the output.

### Step 2: Process unseen sessions (only if unseen_sessions is non-empty)

For each unseen session from the init output:
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

### Step 3: Load summary

Run:
```bash
python <repo_dir>/core/startup.py summary --repo-dir "<repo_dir>"
```

### Step 4: Return summary

Compose and return a message with EXACTLY this structure (no extras):

```
**Identity:** role=<role>, mission=<mission>, registered=<registered>, wants=<wants_role>/<wants_mission>

**Handoffs generated:** <count> — <list of session IDs, or "None">

<paste the full output from the summary command here>
```

Keep the entire output under 300 words.

---

## After launching the agent

Do NOT wait for the agent to finish. Immediately respond to the user's original message. When the agent completes in the background, output its summary prefixed with "Startup complete:" so the user knows session context is loaded.
