# Scribe — Structured Notes and Learnings

The scribe tool (`scribe/notes.py`) manages operational notes and learnings for cross-session continuity. Read this file when you're about to write a note, a learning, a memory entry, or when the user asks you to "remember", "write that down", or "note this."

Project-level rules (portability, CLI lookup, etc.) live in the repo-root `CLAUDE.md`. This file is only about notes, learnings, and memory.

---

## Notes vs Memory vs Learnings

Three separate stores. Pick the right one up front — moving entries between them is manual and tedious.

All three live under the per-target state directory the registry allocates for the current repo (`<apiary>/.repos/<name>-<id>/scribe/` post-C-2026-46). The path resolves automatically when you invoke `scribe/notes.py` via the launcher — you never need to construct it yourself.

| Store | Subpath under `<state-dir>/scribe/` | Lifespan | Good for |
|---|---|---|---|
| **Memory** | `memory/*.md` (indexed via `MEMORY.md`) | Permanent — still true in 3 months | User preferences, project facts, reference patterns, cross-session conventions |
| **Notes** | `<type>/` folder-per-type, indexed via `index.jsonl` | Decays; auto-archived after 30 days | Operational state — TODOs, handoffs, decisions, blockers, wishlists, current-work context |
| **Learnings** | `learnings/` (same folder-per-type structure) | Permanent (no auto-archive) | Project-specific error workarounds, non-obvious patterns, tool quirks you figured out |

**Storage layout.** Notes use a folder-per-type layout. Each note type has its own folder (`todos/`, `handoffs/`, `decisions/`, `wishlists/`, `blockers/`, `references/`, `context/`, `general/`) containing individual `<id>.md` files and an `index.jsonl` for fast listing. Learnings live in `learnings/` with the same structure. Archived notes move into `<type>/archive/` subfolders.

**Quick decision:** Is it still true in 3 months → memory. Is it a workaround or a non-obvious thing I learned → learning. Is it about current work that will decay → note.

---

## Note ID format

Every note and learning has a **TYPE-YEAR-seq** display ID (e.g. `T-2026-1`, `L-2026-3`). The three components:

| Prefix | Type |
|--------|----------|
| `T` | todo |
| `H` | handoff |
| `D` | decision |
| `W` | wishlist |
| `R` | reference |
| `B` | blocker |
| `C` | context |
| `G` | general |
| `L` | learning |

Each **(type, year)** pair has its own independent sequence counter, stored at `<type>/<year>/next_seq` inside the scribe state directory. For example, the first todo created in 2026 is `T-2026-1`, and the first learning in 2026 is `L-2026-1` — their counters are independent.

Legacy bare-integer IDs (e.g. `42`) are still accepted by the CLI via `migration_id_map.json` lookups, but all new notes use TYPE-YEAR-seq format.

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
| User note that does not fit a specific type / miscellaneous capture | `general` | Default bucket when no other type applies |
| Wishlist idea ("would be nice", "eventually", "we should") | `wishlist` | Record the idea |
| Work that matches an active TODO is completed | — | Run `notes.py done <ID>` (where `<ID>` is a TYPE-YEAR-seq ID like `T-2026-1`) |

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

## Note templates

Each note type has a template at `<state-dir>/scribe/templates/<type>.md`, seeded from `scribe/default_templates/` when the repo was bootstrapped. Read one before writing an unfamiliar type:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py template show handoff
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py template list
```

Most templates are **guidance only** — they describe what a good note of that type contains and never block a write. Three declare `required:` sections in their frontmatter, and `notes.py add` rejects content that omits any of them:

| Type | Required sections |
|---|---|
| `handoff` | What was done · Key decisions · What's pending · Where it stopped |
| `decision` | Context · Decision · Why · Consequences |
| `blocker` | Blocked on · Tried · Unblock when |

A section counts as present when it appears as a Markdown heading (`### What was done`) or a bold label (`**Why:**`), case-insensitive. On rejection the CLI prints the template and exits 1 — add the missing sections and re-run. `--force` writes anyway and logs what was missing; use it only when the note genuinely has no such section, not to skip the work. The check runs on `add` only: existing notes are never validated or rewritten, and editing a template only affects notes written after the edit.

To change the shape for this repo, edit `<state-dir>/scribe/templates/<type>.md` — bootstrap never overwrites an existing template.

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

Learnings are stored as individual `.md` files under `<state-dir>/scribe/learnings/`, with a single shared `index.jsonl` for fast listing.

```bash
# Add a learning
python scribe/notes.py learn --content "description of what was learned" --session-id "<sid>"

# List all learnings
python scribe/notes.py learnings [--full] [--search TEXT]

# Remove a stale learning
python scribe/notes.py unlearn <ID>  # e.g. L-2026-3
```

---

## Memory

Memory lives at `<state-dir>/scribe/memory/` (the per-target state dir resolved by the registry — same place notes and learnings live). The directory contains:

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

1. Create `<state-dir>/scribe/memory/<kebab-case-topic>.md` with YAML frontmatter:

    ```markdown
    ---
    name: Short descriptive name
    description: One-line hook used by the startup index
    type: feedback | reference | project
    ---

    Body content here — markdown, as long as needed.
    ```

2. Add a one-line entry to `<state-dir>/scribe/memory/MEMORY.md`:

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

Active notes age into the archive after 30 days; the archive is keyword-searchable but not loaded into startup context. Each note type folder has its own archive subfolder (e.g. `<state-dir>/scribe/todos/archive/`); the `notes.py list --archive` command scans all of them.
