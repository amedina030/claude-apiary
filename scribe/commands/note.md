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

   `decision` and `blocker` notes have required sections (see
   `<state-dir>/scribe/templates/<type>.md`; `add` rejects a note that lacks
   them). Expand a one-liner into those sections before saving — keep the
   user's words, do not invent detail; a section can be one line:
   - decision: `### Context` / `### Decision` / `### Why` / `### Consequences`
   - blocker: `### Blocked on` / `### Tried` / `### Unblock when`
   Pass multi-line content with `--content-file` (write it to a temp file
   first) rather than quoting it on the command line.

   Special subcommand: `/note done <N>` marks note N as done:
   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py done <N>
   ```

2. Run:
   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type <type> --session-id <first 8 chars of session_id> --content "<content>"
   ```
3. Confirm to the user: "Noted: #<id> (<type>)"
