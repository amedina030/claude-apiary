---
name: startup
description: Session startup — generates handoff note and loads active notes
user-invocable: true
---

Run session startup tasks. This skill runs once at the beginning of every session.

## Step 1: Generate/consolidate handoff note

### Determine mode

1. Read `~/.claude/.prev-session.json` to get the previous session ID (use the first 8 characters of `session_id`). If it doesn't exist, try `~/.claude/.last-session.json` instead (but skip if its session_id matches the current session).
2. If no previous session can be determined, output "No previous session found — skipping handoff." and go to Step 2.
3. Run `python <scribe_dir>/notes.py list` and check if the last note is a handoff:
   - If yes → **consolidate** mode with that note's ID
   - If no → **create** mode

Where `<scribe_dir>` is the `scribe/` directory in the claude-apis repo.

### Read the previous session transcript

Read `~/.claude/.last-transcript.jsonl`. Each line is a JSON object with `role` (user/assistant) and `text`.

If the file doesn't exist or is empty, output "No transcript found — skipping handoff." and go to Step 2.

### Analyze the transcript and generate the handoff

Read through the full transcript and produce a structured handoff with these sections:

```
## Session <prev-id> Handoff

### What was done
- Bullet points summarizing completed work (be specific: file names, features, fixes)

### Key decisions
- Decisions made and alternatives rejected (include reasoning)

### What's pending
- Unfinished work, deferred items, open questions

### Where it stopped
- What was happening at the very end of the conversation
```

Guidelines:
- Be concise but specific — file names, function names, concrete details
- Focus on what a future session needs to know to continue the work
- Don't include routine actions (reading files, listing directories) unless they revealed something important
- If the session was short or trivial, the handoff can be brief

### Save the handoff note

**Create mode:**
```bash
python <scribe_dir>/notes.py add --type handoff --session-id <prev-id> --auto --content "<handoff content>"
```

**Consolidate mode:**
Read the existing handoff note #N with `python <scribe_dir>/notes.py get <N>`, merge the new transcript analysis with the existing content (keep the more complete/accurate version of each section), then:
```bash
python <scribe_dir>/notes.py update <N> --content "<merged handoff content>"
```

## Step 2: Read and output active notes and learnings

Run both in parallel:
```bash
python <scribe_dir>/notes.py list
python <scribe_dir>/notes.py learnings
```

Parse the output and present a summary to yourself (this becomes your session context):

1. **Last handoff** — show the most recent handoff note's content (full, not truncated)
2. **Active items** — list all active TODOs, blockers, and other non-handoff notes with their IDs, types, and content
3. **Learnings** — list all learnings (these are project-specific things Claude has learned from past sessions)

If there are no notes, say "No active notes." If there are no learnings, say "No learnings." Then proceed.

## Step 3: Confirm and proceed

Output: "Session initialized." Then respond to the user's actual message.
