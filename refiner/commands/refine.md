---
name: refine
description: Sharpen a fuzzy idea into an airtight requirements spec through adversarial refinement
user-invocable: true
---

# /refine — Idea to Spec

Take a vague idea and turn it into a precise, unambiguous requirements spec. This is a spec-writing tool, not an implementation tool — it produces the *what*, not the *how*. Use plan mode or direct implementation after.

## Arguments

- `/refine <idea>` — start refining the given idea
- `/refine` (no args) — ask the user what they want to build

---

## Step 1: Assess

Read the user's idea and assess how much refinement it needs:

- **Already clear** — purpose, behavior, and boundaries are all specified. Skip to writing the spec. Say: "This is clear enough to spec directly."
- **Partially clear** — some dimensions are obvious, others aren't. Ask only about the gaps. 1 round.
- **Fuzzy** — a direction, not a spec. "Make the budgeter smarter." Full decomposition, 2-3 rounds.

If the idea is trivial (typo fix, single obvious change), tell the user it doesn't need /refine and exit.

---

## Step 2: Refine

You are a demanding product architect. Your job is not to accept the idea — it's to interrogate it until every important decision is made explicitly.

### What to probe

For every idea, systematically check these dimensions. Skip any that are already clear from the user's input.

1. **Purpose** — Why does this need to exist? What problem does it solve? If the user says "add X," ask what breaks or is painful without X.
2. **Inputs and outputs** — What does this thing receive? What does it produce? What format, what types?
3. **Behavior** — What happens step by step? Happy path first. Then: what happens when input is missing? Malformed? When a dependency fails?
4. **Boundaries** — What is this NOT? What's adjacent but explicitly out of scope? If the idea is too big, propose a split.
5. **Constraints** — What must this preserve? What must it not break? Compatibility, performance, style requirements?
6. **Verification** — How will we know it works? What's a concrete test?

### How to ask

- Max 4 questions per round. Prioritize the unknowns that would cause the most damage if guessed wrong.
- **Offer concrete choices, not open prompts.** "Should archived notes go in a separate file or the same file with a status field?" is better than "How should archiving work?" Closed questions converge faster.
- If you see a better approach than what the user described, propose it with a clear reason. Don't just accept the premise.
- If there are competing approaches, lay out the tradeoffs and recommend one.
- Stop when you can write every section of the spec without guessing.

### Rules

- **Describe behavior in terms of user-visible effects, not code internals.** If you know how the codebase works from conversation context, use that to ask better questions — but the spec itself reads like a product requirements doc, not a code diff.
- **Kill scope creep.** The spec should describe one coherent unit of work. If it's growing into multiple features, split it.
- **Be concise.** A good spec is as short as possible while remaining unambiguous. Every sentence should eliminate a potential misunderstanding. If removing a sentence doesn't create ambiguity, remove it.

---

## Step 3: Write the spec

Produce the spec in exactly this format:

```
## Goal
Why this change exists and what it accomplishes (1-2 sentences).

## Behavior
What the feature does, described as inputs → processing → outputs.
Include the happy path and key error/edge cases.
Use concrete examples where they clarify ("given X, produce Y").

## Constraints
- What this must NOT do or break
- Compatibility requirements (existing interfaces, data formats)
- Performance or style requirements, if any

## Scope
- **In:** what's included in this change
- **Out:** what's explicitly excluded (and why, if not obvious)

## Acceptance criteria
- [ ] Concrete, testable condition (happy path)
- [ ] Concrete, testable condition (error case)
- [ ] Concrete, testable condition (edge case)
```

### Self-check before presenting

Before showing the spec to the user, verify against the decomposition dimensions:
- Does Goal state the *why*, not just the *what*?
- Does Behavior cover inputs, outputs, happy path, AND error cases?
- Does Constraints name what must not break?
- Does Scope draw a clear boundary?
- Is every acceptance criterion concrete enough to test mechanically (not "works correctly")?
- Could someone who has never seen the codebase understand this spec?

If any check fails, fix the spec before presenting.

### Acceptance criteria guidelines

Every spec must have criteria covering:
- At least one happy path scenario with concrete input/output
- Each error case mentioned in Behavior
- Boundary conditions (empty input, max size, concurrent access — whichever apply)

### Example: bad spec vs good spec

**Bad** (vague, untestable):
```
## Goal
Add archive support to the notes tool.

## Behavior
Archive old notes. Maybe add a flag.

## Constraints
- Don't break existing notes

## Acceptance criteria
- [ ] Archiving works
```

**Good** (precise, behavior-focused, testable):
```
## Goal
Allow users to move stale notes out of the active list without deleting them,
so the active list stays focused on current work.

## Behavior
A new "archive" operation moves notes from the active store to a separate
archive store. Archived notes no longer appear in default list output.

- Input: a date threshold. All notes older than this date are archived.
- If no date given, default to 30 days ago.
- Output: print count of notes archived and the archive location.
- Archived notes are still searchable via a dedicated flag on the list
  operation (e.g., --archive).
- Archiving is idempotent — running it twice with the same date does nothing
  the second time.

Error cases:
- No notes match the threshold → print "0 notes archived," exit 0.
- Archive store doesn't exist yet → create it automatically.

## Constraints
- Must not modify or delete active notes — only move them
- Archive format must match the active format (same schema) so notes can
  be un-archived later without migration
- Must work with the existing file locking mechanism

## Scope
- **In:** archive command, --archive flag on list, automatic archive creation
- **Out:** un-archive, scheduled auto-archiving, archive pruning/deletion

## Acceptance criteria
- [ ] `archive --before 2025-01-01` moves all notes before that date to archive
- [ ] `list` no longer shows archived notes
- [ ] `list --archive` shows only archived notes
- [ ] `list --archive --search "keyword"` searches within archived notes
- [ ] Running archive twice with same date produces "0 notes archived"
- [ ] Archive file is created automatically on first archive operation
```

---

## Step 4: Present and iterate

Show the user the complete spec. Use AskUserQuestion:

| Option | What happens |
|--------|-------------|
| **Looks good** | Save the spec and finish |
| **Edit** | User provides feedback. Apply it to the existing spec — don't restart. Show only what changed. |

Iterate until the user approves.

---

## Step 5: Save

Save the approved spec as a scribe note for persistence:

```bash
python <repo_dir>/scribe/notes.py add --type context --content "<the full spec text>" --session-id "<session_id>"
```

Tell the user the spec is saved and suggest next steps:
- "Use plan mode to design the implementation"
- "Or just ask me to implement it directly"
