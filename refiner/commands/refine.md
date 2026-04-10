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
1. Run: `python ~/.claude/apiary_launch.py refiner/round_counter.py reset --session-id <session_id>`
2. Respond: "Refinement cancelled. No spec was saved."
3. Stop.

### Start the round counter

```bash
python ~/.claude/apiary_launch.py refiner/round_counter.py start --session-id <session_id>
```

### Docs loading (conditional)

If the user provided a `docs:<path>` prefix:
1. Resolve the apiary repo path: `apiary_repo = output of python ~/.claude/apiary_launch.py --print-repo-path`
2. Read `<apiary_repo>/docs/<path>`. If the file does not exist, tell the user and continue without it.
3. Use the document as architectural context during refinement. Do NOT quote it verbatim in the handoff.

If no `docs:` prefix was given, perform **zero file reads**. Operate from conversation only. **Never read source code files under any circumstance.**

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
   python ~/.claude/apiary_launch.py scribe/notes.py add --type decision \
     --content "KILLED: <original idea>\nReason: <why it was killed>\nSalvageable: <parts worth revisiting, or None>" \
     --session-id "<session_id>"
   ```
4. Say: "Saved a decision note. If the need becomes concrete later, revisit the salvageable parts."
5. Run `round_counter.py reset` and stop.

### Classification

- **Already clear** — purpose, behavior, and boundaries are all specified. Skip to Step 3.
- **Partially clear** — some dimensions obvious, others missing. 1 round of questions.
- **Fuzzy** — a direction, not a spec. 2–3 rounds.

---

## Step 2: Refine

You are a demanding product architect. Your job is to interrogate the idea until every important decision is made explicitly. Reject vague answers — if the user says "it should just work," probe for what "working" means concretely.

### Round management

At the start of each question round, tick the counter:

```bash
python ~/.claude/apiary_launch.py refiner/round_counter.py tick --session-id <session_id>
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

## Step 3: Write the handoff

Produce the handoff in **exactly** this format. Every section and sub-field is required. Do not omit or leave any field empty.

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

### Self-check: 8 validation rules

**Before presenting the handoff to the user, verify every rule below. If any rule fails, fix the handoff — do not present it yet.**

1. Every acceptance criterion references a specific input and observable output — no "works correctly" or "handles gracefully"
2. Every error case in Behavior has a corresponding acceptance criterion
3. Every edge case in Behavior has a corresponding acceptance criterion
4. Shape > Components lists at least one component with a description
5. Shape > Data flow contains at least one arrow (→)
6. Boundaries > Out of scope has a reason for each exclusion
7. Goal > Problem describes a current pain, not a desired future state
8. No field is left empty or filled with a placeholder

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
python ~/.claude/apiary_launch.py scribe/notes.py add --type context \
  --content "<full handoff text>" \
  --session-id "<session_id>"
```

Reset the round counter:

```bash
python ~/.claude/apiary_launch.py refiner/round_counter.py reset --session-id <session_id>
```

Tell the user the spec is saved and suggest next steps:
- "Use plan mode to design the implementation"
- "Or just ask me to implement it directly"
