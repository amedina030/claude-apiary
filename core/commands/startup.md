---
name: startup
description: Session startup — declares identity, generates handoffs, loads notes
user-invocable: true
---

Launch a startup agent to handle session initialization. Do NOT execute these steps yourself — delegate them entirely to the agent.

## What to do

Spawn an agent (subagent_type: "general-purpose", run_in_background: true) with the following prompt. Replace `<repo_dir>` with the current repo working directory, and `<session_id>` with the current session ID from budgeter context (first 8 chars).

Also pass the **full text of the user's first message** to the agent as `<first_message>`.

---

**Agent prompt:**

You are a session startup agent. Your job is to declare session identity, generate handoff notes from previous sessions, and load active notes/learnings. Return a concise summary at the end — keep it short, this output will persist in the main conversation context.

### Step 1: Declare session identity

1. Check if `<first_message>` matches a structured identity format. Look for fields like `role:`, `mission:`, `wants:` in the message. Example structured message:
   ```
   role: attacker
   mission: project X
   wants: defender project X
   ```

2. If structured format is found, parse `role`, `mission`, and `wants` (where wants is `{role, mission}`).
   If NOT structured, default to: `role: user`, `mission: general`, `wants: {role: user, mission: general}`.

3. Validate role and mission against the registry at `<repo_dir>/core/config/session-registry.json` (contains `{"roles": [...], "missions": [...]}`). Set `registered: true` if both values are in the registry, `registered: false` if either is not.

4. Write the identity file (use Python so it matches the pre-approved permission pattern):
   ```bash
   python -c "import json,pathlib; pathlib.Path(pathlib.Path.home()/'.claude'/'.session-identity-<session_id>.json').write_text(json.dumps({'role':'<role>','mission':'<mission>','registered':<true|false>,'wants_role':'<wants.role>','wants_mission':'<wants.mission>'}))"
   ```

### Step 2: Generate handoff notes for unseen sessions

1. Read `~/.claude/.session-history.json` to get the array of recent sessions. If missing or empty, skip to Step 3.

2. Filter the history:
   - Remove entries where `session_id` starts with `<session_id>` (current session)
   - Keep entries where `role` matches this session's `wants_role` AND `mission` matches `wants_mission`
   - These are "matching sessions"

3. Get existing handoff session IDs: run `python <repo_dir>/scribe/notes.py list --type handoff` and extract the session IDs from handoff notes.

4. Filter matching sessions to only "unseen" ones — those whose `session_id` (first 8 chars) does NOT appear in any existing handoff's session ID.

5. For each unseen matching session (oldest first):
   a. Get its `transcript_path`. Run `python <repo_dir>/core/hooks/extract_transcript.py <transcript_path>` to extract clean messages (each line: JSON with `role` and `text`). If output is empty, skip this session.
   b. Analyze the transcript and produce a handoff with these sections:
      ```
      ## Session <prev-id> Handoff
      **Role:** <role> | **Mission:** <mission>
      ### What was done
      ### Key decisions
      ### What's pending
      ### Where it stopped
      ```
      Be concise but specific — file names, function names, concrete details. Focus on what a future session needs.
   c. Save the handoff:
      ```bash
      python <repo_dir>/scribe/notes.py add --type handoff --session-id <prev-id> --auto --if-no-handoff-for <prev-id> --content "<handoff>"
      ```

### Step 3: Load active notes and learnings

Run in parallel:
```bash
python <repo_dir>/scribe/notes.py list
python <repo_dir>/scribe/notes.py learnings
```

### Step 4: Return summary

Return a message with EXACTLY this structure (no extras):

```
**Identity:** role=<role>, mission=<mission>, registered=<true|false>, wants=<wants_role>/<wants_mission>

**Handoffs generated:** <count> — <list of session IDs, or "None">

**Active items:** <count> notes — <brief list of IDs and types, e.g. "#5 todo, #6 wishlist, #7 todo">

**Learnings:** <count> — <brief list or "None">
```

Keep the entire output under 200 words. Do NOT include full note contents — just IDs, types, and a few words each.

---

## After launching the agent

Do NOT wait for the agent to finish. Immediately respond to the user's original message. When the agent completes in the background, output its summary prefixed with "Startup complete:" so the user knows session context is loaded.
