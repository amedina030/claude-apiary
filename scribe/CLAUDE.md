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
| **Notes** | `<type>/` folder-per-type, indexed via `index.jsonl` | Decays; auto-archived per type (see below) | Operational state — TODOs, handoffs, decisions, blockers, wishlists, current-work context |
| **Learnings** | `learnings/` (same folder-per-type structure) | Permanent (no auto-archive) | Project-specific error workarounds, non-obvious patterns, tool quirks you figured out |

**Storage layout.** Notes use a folder-per-type layout. Each note type has its own folder (`todos/`, `handoffs/`, `decisions/`, `wishlists/`, `blockers/`, `references/`, `context/`, `general/`) containing individual `<id>.md` files and an `index.jsonl` for fast listing. Learnings live in `learnings/` with the same structure. Archived notes move into `<type>/<year>/archive/`.

**Quick decision:** Is it still true in 3 months → memory. Is it a workaround or a non-obvious thing I learned → learning. Is it about current work that will decay → note.

### Retention: what auto-archives, and when

The rules live in `scribe/policy.py` and run from `notes.py add`, `notes.py tidy`, and session startup — never from `list`, which is read-only.

| Type | Archived when |
|---|---|
| `handoff` | A newer handoff for the same role/mission exists |
| `context` | 3 days old |
| `decision` | 30 days old |
| *(any type)* marked `done` | 1 day after it was **marked done**, not after it was written |
| `todo`, `wishlist`, `blocker`, `reference`, `general` | Never on age — only once closed |

Archiving is not deletion: `notes.py list --archive` searches it and `notes.py unarchive <ID>` brings a note back.

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

TYPE-YEAR-seq is the only ID form the CLI accepts — legacy bare-integer IDs (e.g. `42`, `L3`) from the pre-2026-04 store were retired along with their migration map.

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

Learnings are stored as individual `.md` files under `<state-dir>/scribe/learnings/`, one per year with a shared `index.jsonl` for fast listing. Always invoke through the launcher, so the per-target state dir resolves:

```bash
L="$(git rev-parse --show-toplevel)/.claude/apiary/launch.py"

# Add a learning. --tags is optional; see "Tagging" below.
python "$L" scribe/notes.py learn --content "what you learned" --session-id "<sid>"

# List all learnings
python "$L" scribe/notes.py learnings [--full] [--search TEXT] [--tag TAG]

# Retire a stale learning (reversible — the .md moves to archive/)
python "$L" scribe/notes.py archive-learning <ID>   # e.g. L-2026-3

# Replace one with an updated version that records the lineage
python "$L" scribe/notes.py supersede <ID> --content "<new content>"
```

`unlearn <ID>` also exists and **hard-deletes** the body and its row. Prefer `archive-learning`: it is the same retirement, and it is reversible.

**Tagging.** `learn` does not call a model. Pass `--tags a,b` when you already know the right tags; leave them off otherwise and let `/review-learnings` run `notes.py retrotag` to fill the gaps in one batch. `--infer` opts a single command into inference, and `APIARY_SCRIBE_INFER=1` opts in a whole session. This is why `/wrapup` is fast: writing a learning never spawns a subprocess unless you asked it to.

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
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py list --archive --search "<keyword>"
```

Notes age into the archive on the per-type schedule above; the archive is keyword-searchable but not loaded into startup context. Each year folder has its own archive subfolder (e.g. `<state-dir>/scribe/todos/2026/archive/`); `notes.py list --archive` scans all of them, and `notes.py unarchive <ID>` brings one back.

---

## Module map

`scribe/notes.py` is the command line only — argparse, one `cmd_*` per verb, and the printing. When you are changing behaviour, the file you want is usually one of these:

| Module | Owns |
|---|---|
| `store.py` | The storage engine: folders, `index.jsonl`, `<seq>.md` bodies, `next_seq`. Every read-modify-write of an index runs inside one `_locked_index` hold |
| `policy.py` | Retention — what auto-archives and when, as pure functions over index rows |
| `maintenance.py` | Whole-store operations: `repair`, `backfill-brief`, `backup`/`restore`, `retrotag`, `mark-reviewed` |
| `templates.py` | Per-type templates and the required-section gate on `add` |
| `infer.py` | Tag/area inference — the only place scribe calls a model, and it is off by default |
| `formatting.py` | Display IDs, relative ages, colour, the list and detail renderers |
| `paths.py` | Where the state dir is, and the session identity a write inherits |
| `cli_args.py` | The argparse declaration and the helpers that read values back out of it |
| `api.py` | The frozen public API external tools import (`open_store`, `normalize_entry`, …) |
| `backup_indexes.py` | The older entry point for `notes.py backup`; delegates to `maintenance.py` |
