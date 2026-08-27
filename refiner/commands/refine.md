---
name: refine
description: Refine a fuzzy idea into a structured handoff spec through value-first adversarial questioning
user-invocable: true
---

# /refine — Idea to Handoff

Take a vague idea and turn it into a structured, implementable handoff document. This is a spec-writing tool, not an implementation tool — it produces the *what*, not the *how*. Use plan mode or direct implementation after.

## Arguments

- `/refine <idea>` — start refining the given idea
- `/refine` (no args) — ask the user what they want to build
- `/refine cancel` — cancel the current refinement and exit
- `/refine docs:<path> <idea>` — start with a docs file loaded for orientation (e.g. `docs:standards/code-style.md`)

---

## Step 0: Setup and guards

Run these checks before any user-facing output.

### Cancel

If the argument is `cancel`:
1. Run: `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py reset --session-id refine-<session_id>`
2. Respond: "Refinement cancelled. No spec was saved."
3. Stop.

### Start the round counter

`/refine` and `/harden` share one round-counter tool. The `refine-` prefix on
`--session-id` keeps the two counters in separate state files when both run in
the same session.

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py start --session-id refine-<session_id>
```

### Docs loading (conditional)

If the user provided a `docs:<path>` prefix:
1. Resolve the apiary repo path: `apiary_repo = output of python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" --print-repo-path`
2. Read `<apiary_repo>/docs/<path>`. If the file does not exist, tell the user and continue without it.
3. Use the document as architectural context during refinement. Do NOT quote it verbatim in the handoff.

If no `docs:` prefix was given, perform **zero file reads during Steps 1–2**. Assessment and questioning operate from conversation only — do not read source code while interrogating the idea. Codebase grounding happens later, in its own read-only pass (Step 2.5), once the *what* is settled.

---

## Step 1: Assess

### Trivial detection

If the idea is a single obvious change with no ambiguity (typo fix, rename, toggle a flag):
- Say: "This is too simple for /refine — just ask me to do it."
- Run `round_counter.py reset` and stop.

### Too-large detection

If the idea spans multiple independent features that could each be a separate handoff:
- Say: "This is too large to refine in one pass. Here are the natural pieces: [list]."
- Ask which piece to start with. Proceed with one piece only.

### Value challenge

Before probing HOW, challenge WHY. Ask: **"What breaks or is painful today without this?"**

If the user cannot articulate a concrete problem after one direct challenge, trigger the idea-kill flow.

### Idea-kill flow

When the user cannot state the problem this solves:
1. Declare the idea insufficiently grounded.
2. Ask what parts, if any, are worth revisiting.
3. Save a decision note:
   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type decision \
     --content "KILLED: <original idea>\nReason: <why it was killed>\nSalvageable: <parts worth revisiting, or None>" \
     --session-id "<session_id>"
   ```
4. Say: "Saved a decision note. If the need becomes concrete later, revisit the salvageable parts."
5. Run `round_counter.py reset` and stop.

### Classification

- **Already clear** — purpose, behavior, and boundaries are all specified. Skip to Step 2.5.
- **Partially clear** — some dimensions obvious, others missing. 1 round of questions.
- **Fuzzy** — a direction, not a spec. 2–3 rounds.

---

## Step 2: Refine

You are a demanding product architect. Your job is to interrogate the idea until every important decision is made explicitly. Reject vague answers — if the user says "it should just work," probe for what "working" means concretely.

### Round management

At the start of each question round, tick the counter:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py tick --session-id refine-<session_id>
```

If the returned count is **15**:
- Ask: "We've reached 15 rounds. Continue refining (I'll reset the counter) or produce the handoff with what we have?"
- If continue → run `round_counter.py reset`, resume questioning.
- If produce → skip to Step 3, noting any fields that couldn't be fully resolved.

### What to probe

Systematically check these dimensions, skipping any already clear:

1. **Purpose** — Why does this need to exist? What is painful without it?
2. **Inputs and outputs** — What does it receive? What does it produce? Format, types?
3. **Behavior** — Step by step: happy path first, then error and edge cases.
4. **Boundaries** — What is this NOT? What's adjacent but out of scope? If too big, propose a split.
5. **Constraints** — What must this preserve? Compatibility, performance, style?
6. **Verification** — How will we know it works? Concrete tests?

### How to ask

- **2–4 questions per round** (soft guideline). Prioritize unknowns that would cause the most damage if guessed wrong.
- **Offer concrete choices, not open prompts.** "Should archived notes go in a separate file or the same file with a status field?" beats "How should archiving work?"
- If you see a better approach than what the user described, propose it with reasoning. Don't just accept the premise.
- If there are competing approaches, lay out the tradeoffs and recommend one.
- **Stop when you can write every section of the handoff without guessing.**

---

## Step 2.5: Ground in the codebase

Once the *what* is settled, anchor the spec to the real codebase **before** writing the handoff. This is a read-only verification pass — it confirms what already exists so the spec cites real functions, paths, and patterns. It does **not** design the implementation (still "what, not how").

### When to run

- **Run** for any idea that touches code in the session's repo (a new tool, a change to an existing module, a behavior that plugs into existing code).
- **Skip** for ideas with no codebase footprint (a pure process/workflow change, a docs-only task, external infra). If you skip, write the handoff from conversation and tag would-be code references as `(new)`.

### How to ground

Dispatch a **single read-only `Explore` subagent** (agentType `Explore`) against the session repo. Give it the settled idea and ask it to return, as a structured summary:

1. **Integration point** — the real file(s)/module(s) where this would plug in, by path.
2. **Existing patterns** — established patterns, base classes, helpers, or conventions this should follow, each with the file/symbol that exemplifies it.
3. **Real symbols and paths** — concrete function/class/constant names and file paths the acceptance criteria can reference.
4. **Adjacent prior art** — anything already in the repo that overlaps, or that this change must not break.
5. **Conflicts & surprises** — anything that contradicts the idea as described, makes it redundant (already solved by existing code), exposes a cleaner approach, or reveals an invariant the change would break. Ask for this explicitly — it is the signal that decides whether you write or loop back.

The subagent is read-only — it must not edit anything. If it finds nothing relevant (genuinely greenfield), treat the idea as a `(new)` pattern and proceed.

### When grounding changes the picture

Grounding can invalidate what questioning settled. Judge the findings before writing — do **not** paper over a conflict to keep moving:

- **Confirms or enriches** (the common case) — the idea holds; the facts just make it concrete. Carry them into Step 3.
- **Contradicts a premise** — a pattern, module, or symbol the user assumed doesn't exist or works differently. Loop back to Step 2: tick the round counter, state the contradiction in plain language, and resolve it with the user before writing.
- **Already solved / strong prior art** — existing code already does most of this. Surface it; if it's fully covered, run the idea-kill flow; if only partial, re-scope with the user to the remaining gap.
- **A cleaner approach appears** — grounding reveals a better integration point or an existing helper to build on. Propose it with reasoning (Step 2's "don't just accept the premise" rule) and let the user choose.
- **New hard constraint or scope blowup** — the change must preserve an invariant the user never mentioned, or it touches far more than assumed. Fold a genuine invariant into Boundaries > Must not break; if scope explodes, trigger the too-large split.

Re-ground only if the resolution materially moves the integration point or pattern — otherwise carry the original facts forward. The 15-round cap still backstops any loop.

---

## Step 3: Write the handoff

Produce the handoff in **exactly** this format. Every section and sub-field is required. Do not omit or leave any field empty.

**Use the grounding facts from Step 2.5.** Shape > Integration point, Pattern, and Dependencies must name the real files/symbols/patterns found; acceptance criteria must reference real paths and symbols wherever the scenario touches existing code. Anything that does not yet exist must be tagged `(new)` so it is unambiguous which references are real and which are to-be-built.

```
## Goal
- **Problem:** What is broken or painful today (1 sentence)
- **Solution:** What this change does about it (1 sentence)
- **Value:** Who benefits and how (1 sentence)

## Shape
- **Components:**
  - [Name]: [What it does and why it exists] (one per component)
- **Integration point:** Where this plugs into the existing system
- **Pattern:** What existing pattern it follows, or "new pattern" if none
- **Data flow:** Input source → processing steps → output destination
- **Dependencies:** What this requires to exist/work (if applicable)

## Behavior
- **Input:** What the feature receives (type, format, source)
- **Processing:** Ordered steps from input to output
- **Output:** What the feature produces (type, format, destination)
- **Error cases:**
  - [trigger] → [expected behavior] (one per known error)
- **Edge cases:**
  - [condition] → [expected behavior] (one per known edge)

## Boundaries
- **In scope:** Bulleted list of what is included
- **Out of scope:** Bulleted list of what is excluded, each with a reason
- **Must not break:** Bulleted list of invariants this change must preserve

## Acceptance criteria
- [ ] Given [precondition], when [action], then [observable result]
(one per happy-path scenario, one per error case, one per edge case)
```

### Self-check: 9 validation rules

**Before presenting the handoff to the user, verify every rule below. If any rule fails, fix the handoff — do not present it yet.**

1. Every acceptance criterion references a specific input and observable output — no "works correctly" or "handles gracefully"
2. Every error case in Behavior has a corresponding acceptance criterion
3. Every edge case in Behavior has a corresponding acceptance criterion
4. Shape > Components lists at least one component with a description
5. Shape > Data flow contains at least one arrow (→)
6. Boundaries > Out of scope has a reason for each exclusion
7. Goal > Problem describes a current pain, not a desired future state
8. No field is left empty or filled with a placeholder
9. Every file, symbol, or pattern named in Shape or Acceptance criteria either resolves to real code found during grounding (Step 2.5) or is explicitly tagged `(new)` — no invented or unverified references

---

## Step 4: Present and iterate

Show the complete handoff to the user. Use AskUserQuestion:

| Option | Action |
|--------|--------|
| **Approved** | Proceed to Step 5 |
| **Edit needed** | User provides feedback. Apply edits to the existing handoff — don't restart. Show only what changed. Re-run the 8 validation rules after every edit. |

Iterate until the user approves.

---

## Step 5: Save

Save the approved handoff as a scribe note:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type context \
  --content "<full handoff text>" \
  --session-id "<session_id>"
```

Reset the round counter:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py reset --session-id refine-<session_id>
```

Tell the user the spec is saved and suggest next steps:
- "Use plan mode to design the implementation"
- "Or just ask me to implement it directly"
