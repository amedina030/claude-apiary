---
name: runner-prep
description: Audit active scribe todos and prepare automation-ready intake JSONs for the runner pipeline, with dependency ordering and safety classification
user-invocable: true
---

# /runner-prep — Stage todos for runner execution

Look at the active scribe todos and prepare runner intake files for the ones that are ready to be automated. The goal is to land well-scoped intake JSONs in `runner/intake/` with a safety read per ticket and explicit dependency ordering, without promoting anything to the backlog (that is the user's call).

## Arguments

- `/runner-prep` (no args) — run the full pass over all active todos.
- `/runner-prep T-YYYY-NN[,T-YYYY-MM,...]` — restrict the pass to a specific comma-separated list of todo IDs.

---

## Scope rules

- Only touch **active** scribe todos (skip deferred, done, archived).
- Skip todos that are **design-heavy or fuzzy** — key decisions not yet made, shape not yet concrete. Flag those separately so the user knows what still needs their input before it can be specced.
- Skip todos explicitly marked as "idea only" or intentionally parked for later revisit.
- Do not promote anything to `runner/backlog/`. Promotion is the user's call.

---

## Step 1: Enumerate

List the active scribe todos:

```bash
python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" scribe/notes.py list --type todo
```

For any todo whose body is not already visible, read it with:

```bash
python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" scribe/notes.py get T-YYYY-NN
```

If the user passed a restricted ID list as the argument, operate only on that subset.

---

## Step 2: Triage each todo into one of four buckets

- **Ready to prep** — problem, shape, scope, and acceptance criteria are all present or derivable from the todo body without making new decisions. Go to Step 3.
- **Needs user input** — has unresolved decisions that block specifying (examples: API shape not chosen, design direction not agreed, external dependency not resolved). Collect the specific questions; do not prep yet.
- **Design-heavy / fuzzy** — too exploratory to prep at this point (examples: "investigate whether X is a good idea", "revisit Y pattern later"). Skip and list with a one-line reason.
- **Intentionally parked** — the todo itself says "idea only," "revisit later," or similar. Skip.

Do not ask the user questions mid-pass. Collect everything into the report at the end.

---

## Step 3: Dependency analysis

For the ready-to-prep set:

- Read cross-references inside each todo body (phrases like "depends on T-XYZ", "prerequisite: T-XYZ", "blocks T-XYZ", "see C-YYYY-NN for rationale").
- Cross-reference the `scope` fields for overlapping file paths — two todos touching the same file are implicit dependencies for ordering purposes.
- Build a partial order. Record each dependency explicitly in the intake's `context` field so the runner executor can respect it.

---

## Step 4: Write one intake JSON per ready-to-prep todo

For each todo in the ready-to-prep bucket, write a file to `runner/intake/<uuid>.json` with the schema used by `refine_to_intake.py`:

```json
{
  "id": "<uuid4>",
  "title": "<one-line title from todo summary>",
  "problem": "<what is broken or painful today>",
  "description": "<SHAPE + BEHAVIOR, inline, merged>",
  "scope": "<files/dirs touched, test conventions, stdlib-only if applicable>",
  "context": "<acceptance criteria + dependencies + safety tag + rationale>",
  "created_at": "<ISO8601 UTC>",
  "source": "scribe-todo:T-YYYY-NN"
}
```

### Safety tag

Every intake must carry a safety read inside its `context` field on its own line:

- `SAFETY: safe-for-unattended` — mechanical, reversible, no external side effects. Runner can take it overnight.
- `SAFETY: external-side-effect` — touches git remotes, external APIs, messages, or shared state. Runner should gate on human approval.
- `SAFETY: destructive` — deletes files, drops state, force-pushes, or otherwise hard-to-reverse. Runner should NOT take this unattended.

This is descriptive only; it does not gate promotion. It is metadata for the user to filter on when deciding what to promote to backlog.

### Validate

After writing each intake, validate it:

```bash
python -m runner.validate_intake runner/intake/<uuid>.json
```

If validation fails, delete the file and record the failure in the final report rather than asking the user mid-pass.

---

## Step 5: Update each source todo with a back-pointer

For every todo that was successfully prepped, append a back-pointer line to its body so the user can find the intake later:

```bash
python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" scribe/notes.py update T-YYYY-NN \
  --content "<original body>

---
PREPPED 2026-MM-DD — intake: runner/intake/<uuid>.json (SAFETY: <tag>)"
```

The original body must be preserved verbatim; only append.

---

## Step 6: Single end-of-pass report

Produce one markdown report for the user at the end. Do not present partial results. Use the following structure:

```
## /runner-prep report — YYYY-MM-DD

### Prepped (N)
| Todo | Intake | Safety |
|---|---|---|
| T-YYYY-NN | runner/intake/<uuid>.json | safe-for-unattended |
| ... |

### Needs your input (N)
- **T-YYYY-NN** — <one-line summary>
  - Decisions blocking the prep:
    - <specific question 1>
    - <specific question 2>

### Skipped — design-heavy / fuzzy (N)
- T-YYYY-NN — <one-line reason>

### Skipped — intentionally parked (N)
- T-YYYY-NN — <reason from todo body>

### Dependency graph (if any dependencies found)
- T-YYYY-NN depends on T-YYYY-MM (reason)
- ...

### Validation failures (if any)
- runner/intake/<uuid>.json — <error>
```

Each bucket that is empty can be omitted.

---

## Step 7: Stop

Do not promote. Do not run the runner. Do not ask follow-up questions. The user decides what to do with the report.
