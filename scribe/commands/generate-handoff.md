---
name: generate-handoff
description: Generate or consolidate a handoff note from the previous session transcript. Invoked automatically by the load_notes hook at session start.
user-invocable: true
---

Generate a handoff note from the previous session's transcript.

## Arguments

This skill receives arguments in one of two forms:

- `create --prev-id <ID>` — Create a new handoff note from the transcript
- `consolidate --prev-id <ID> --handoff-id <N>` — Consolidate the transcript with existing handoff note #N

## Steps

1. **Spawn a subagent** (using the Agent tool) with the following instructions:

   > You are generating a handoff note from a previous Claude Code session transcript.
   >
   > **Mode: `create`** — Read the transcript and write a new handoff note.
   > **Mode: `consolidate`** — Read the transcript AND the existing handoff note, merge into one.
   >
   > ### What to do
   >
   > 1. Read `~/.claude/.last-transcript.jsonl` — this is the stripped conversation transcript from session `<prev-id>`.
   >    Each line is JSON with `role` and `text` fields.
   >
   > 2. If consolidating, also read the existing handoff note:
   >    ```bash
   >    python <scribe_dir>/notes.py get <handoff-id>
   >    ```
   >
   > 3. Write a summary covering:
   >    - **What was done** — key accomplishments, changes made, tests passing
   >    - **What's pending** — open questions, deferred work, unresolved issues
   >    - **Where it stopped** — the last topic being discussed, any staged/undelivered answers
   >    - **Key decisions** — important choices made and why
   >
   >    Keep it concise but complete enough to resume work without re-reading the full transcript.
   >    Do NOT include routine tool calls or file reads — focus on substance.
   >
   > 4. Write the note:
   >    - **Create mode:**
   >      ```bash
   >      python <scribe_dir>/notes.py add --type handoff --session-id <prev-id> --auto --content "<summary>"
   >      ```
   >    - **Consolidate mode:**
   >      ```bash
   >      python <scribe_dir>/notes.py update <handoff-id> --content "<consolidated summary>"
   >      ```
   >
   > Where `<scribe_dir>` is the `scribe/` directory in the claude-apis repo (`D:/Professional/claude-apis/scribe`).

2. Wait for the subagent to finish, then proceed with the user's request.
