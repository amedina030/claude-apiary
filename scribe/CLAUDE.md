# Scribe — Structured Notes and Learnings

The scribe tool (`scribe/notes.py`) manages operational notes and learnings for cross-session continuity. Read this file when you're about to write a note, a learning, a memory entry, or when the user asks you to "remember", "write that down", or "note this."

Project-level rules (portability, CLI lookup, etc.) live in the repo-root `CLAUDE.md`. This file is only about notes, learnings, and memory.

---

## Notes vs Memory vs Learnings

Three separate stores. Pick the right one up front — moving entries between them is manual and tedious.

| Store | Location | Lifespan | Good for |
|---|---|---|---|
| **Memory** | `<repo-root>/.apiary/scribe/memory/*.md` (indexed via `MEMORY.md`) | Permanent — still true in 3 months | User preferences, project facts, reference patterns, cross-session conventions |
| **Notes** | `<repo-root>/.apiary/scribe/notes.jsonl` via `scribe/notes.py` | Decays; auto-archived after 30 days | Operational state — TODOs, handoffs, decisions, blockers, wishlists, current-work context |
| **Learnings** | `<repo-root>/.apiary/scribe/learnings.jsonl` via `scribe/notes.py learn` | Permanent (no auto-archive) | Project-specific error workarounds, non-obvious patterns, tool quirks you figured out |

> **Transition status.** The canonical paths above are selected when `APIARY_STATE_LAYOUT=repo` is set. Until todo #268 flips the default, the legacy location is `~/.claude/projects/claude-apiary/{notes,notes_archive,learnings}.jsonl` and `~/.claude/projects/claude-apiary/memory/`. The data was mirrored into the new layout in step #266; writes currently still land in the legacy store. Decision #269 tracks the migration.

**Quick decision:** Is it still true in 3 months → memory. Is it a workaround or a non-obvious thing I learned → learning. Is it about current work that will decay → note.

---

## When to write a note

Notes are primarily for Claude's own use — to maintain continuity across sessions. The user does not usually read them.

### User-triggered

| Signal | Type | Action |
|---|---|---|
| User defers work ("later", "hold on", "next time") | `todo` | Write a TODO with enough context to resume |
| Design choice resolved, alternatives rejected | `decision` | Record what was decided AND what was rejected (and why) |
| Something blocks progress | `blocker` | Record what's blocked and why |
| User says "note this" / "write that down" | as specified, or `context` | Follow the user's lead on type |
| Wishlist idea ("would be nice", "eventually", "we should") | `wishlist` | Record the idea |
| Work that matches an active TODO is completed | — | Run `notes.py done <id>` |

### Self-triggered (Claude writes without prompting)

| Signal | Type | Action |
|---|---|---|
| Context is getting large, key state at risk of compaction | `context` | Save critical decisions, open questions, current approach |
| Session ending / user asks for handoff | `learnings + todos` first, then handoff | **Review the entire session** for non-obvious discoveries, workarounds, deferred work, and untracked bugs. Write any missing learnings/TODOs **before** writing the handoff. This is the primary knowledge-capture mechanism — do not skip it. |
| Defer a side-fix to avoid scope creep | `todo` | Write it down immediately, not at session end |
| Discover a workaround / non-obvious pattern | learning | Write it immediately via `notes.py learn`, not at session end |

### Do NOT write a note for

- Routine tool calls or file reads
- Information that belongs in memory (permanent facts about user/project)
- Things already captured by git (file changes, commit history)
- Ephemeral conversation details that won't matter next session
- A direct answer to a user question (that's output, not persistent state)

---

## When to write a learning

Learnings are project-specific things you discover during task execution. They persist indefinitely (no auto-archive) and are loaded into every session startup context.

**Write a learning when:**

- You hit an error and found a workaround (encoding issues, platform quirks, tool quirks)
- You discovered a better approach mid-task than what you initially tried
- A tool or API behaved unexpectedly and you figured out why
- You found a non-obvious project-specific pattern or constraint

**Do NOT write a learning when:**

- The fix is obvious from the error message
- It's general programming knowledge (not project-specific)
- It's already documented in the codebase, `docs/`, or `CLAUDE.md`
- It duplicates an existing learning — update the existing one instead

### Learning commands

```bash
# Add a learning
python scribe/notes.py learn --content "description of what was learned" --session-id "<sid>"

# List all learnings
python scribe/notes.py learnings [--full] [--search TEXT]

# Remove a stale learning
python scribe/notes.py unlearn <id>
```

---

## Memory

Memory lives at `<repo-root>/.apiary/scribe/memory/` (inside the repo checkout, under the umbrella `.apiary/` state dir). The directory contains:

- `MEMORY.md` — one-line index pointing to each memory file. Loaded into startup context.
- `<topic>.md` — one file per distinct memory entry. Filename is `kebab-case-description.md`.

**When to add a memory entry:**

- You learned a durable fact about the user (their role, strong preference, how they want things framed)
- A project-wide convention was decided and documented nowhere else
- A reference pattern that Claude will need across many sessions

**When NOT to add a memory entry:**

- The fact belongs in `docs/` or `README.md` — put it there instead
- It's operational, about current work, or will decay — use a note
- It's a one-off finding from a single task — use a learning

### Writing a new memory entry

1. Create `<repo-root>/.apiary/scribe/memory/<kebab-case-topic>.md` with YAML frontmatter:

    ```markdown
    ---
    name: Short descriptive name
    description: One-line hook used by the startup index
    type: feedback | reference | project
    ---

    Body content here — markdown, as long as needed.
    ```

2. Add a one-line entry to `<repo-root>/.apiary/scribe/memory/MEMORY.md`:

    ```markdown
    - [Short name](kebab-case-topic.md) — one-line hook
    ```

3. Do not duplicate existing memory files — edit them in place.

---

## Archive fallback

If a note the user references isn't found in active notes, search the archive:

```bash
python scribe/notes.py list --archive --search "<keyword>"
```

Active notes age into the archive after 30 days; the archive is keyword-searchable but not loaded into startup context.
