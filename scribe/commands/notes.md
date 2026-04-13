---
name: notes
description: List and retrieve notes from the scribe tool
user-invocable: true
---

Query notes using the scribe tool.

## Argument parsing

- `/notes` → `notes.py list`
- `/notes all` → `notes.py list --all`
- `/notes todo` → `notes.py list --type todo`
- `/notes handoff` → `notes.py list --type handoff`
- `/notes decision` → `notes.py list --type decision`
- `/notes wishlist` → `notes.py list --type wishlist`
- `/notes blocker` → `notes.py list --type blocker`
- `/notes reference` → `notes.py list --type reference`
- `/notes context` → `notes.py list --type context`
- `/notes learning` → `notes.py list --type learning`
- `/notes search <keyword>` → `notes.py list --search "<keyword>"`
- `/notes session <id>` → `notes.py list --session "<id>"`
- `/notes last <N>` → `notes.py list --last <N>`
- `/notes archive` → `notes.py list --archive`
- `/notes <ID>` (where `<ID>` is a TYPE-YEAR-seq ID like `T-2026-1`, or a legacy bare integer) → `notes.py get <ID>`

## Steps

1. Parse the arguments above.
2. Run the appropriate command:
   ```bash
   python ~/.claude/apiary_launch.py scribe/notes.py <subcommand> [args]
   ```
3. Display the output to the user.
