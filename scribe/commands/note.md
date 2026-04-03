---
name: note
description: Add a note via the scribe tool
user-invocable: true
---

Add a structured note using the scribe tool.

## Steps

1. Parse the user's input to determine note type and content.

   Type detection from content prefix (case-insensitive):
   - Starts with `TODO:` → type `todo`
   - Starts with `HANDOFF` → type `handoff`
   - Starts with `DECISION:` → type `decision`
   - Starts with `WISHLIST:` → type `wishlist`
   - Starts with `REFERENCE:` → type `reference`
   - Starts with `BLOCKER:` → type `blocker`
   - Otherwise → type `context`

   If the user specifies a type explicitly (e.g. `/note --type todo Fix the bug`), use that.

   Special subcommand: `/note done <N>` marks note N as done:
   ```bash
   python <scribe_dir>/notes.py done <N>
   ```

2. Run:
   ```bash
   python <scribe_dir>/notes.py add --type <type> --session-id <first 8 chars of session_id> --content "<content>"
   ```
   Where `<scribe_dir>` is the `scribe/` directory in the claude-apiary repo.

3. Confirm to the user: "Noted: #<id> (<type>)"
