---
name: apiary-context
description: Load apiary toolkit context (scribe, budgeter, runner, portability rules)
user-invocable: true
---

This environment uses **claude-apiary** -- a toolkit extending Claude Code with **budgeter**, **scribe**, **refiner**, **harden**, **runner**, and shared **core** infrastructure.

---

## CLI tool lookup

When in doubt about a CLI tool's flags, look it up rather than guessing:

```bash
python docs/reference/cli_lookup.py <tool>   # e.g. notes, report, round_counter
```

---

## Handoff backfilling

If `unseen_sessions` (injected by a PreToolUse hook on the first tool call) is anything other than `none`, run `/backfill-handoffs`. If `none`, skip it.

When the backfill agent's task notification arrives, pipe its `<usage>` block to the cost logger silently:

```bash
echo '<usage>...</usage>' | python $CLAUDE_PROJECT_DIR/budgeter/log_agent_cost.py --session-id "<full_session_id>" --agent "backfill-handoffs" --cwd "$CLAUDE_PROJECT_DIR"
```

---

## Memory path

Canonical memory directory: `<repo-root>/.apiary/scribe/memory/`. Do not write to the cwd-derived harness path.

---

## Portability

All apiary code must be portable across Windows / macOS / Linux. **Read `PORTABILITY.md` before modifying code, hooks, scripts, or `settings.json` entries.**

---

## Notes, learnings, and memory

Apiary uses **scribe** (`scribe/notes.py`) for operational state (TODOs, handoffs, decisions, blockers, wishlists) and a **memory** directory for permanent facts.

**Read `scribe/CLAUDE.md` before writing notes, learnings, or making memory decisions.** Quick decision tree:

- Still true in 3 months -> **memory** (`<repo-root>/.apiary/scribe/memory/`)
- Decays / operational / about current work -> **note** (`scribe/notes.py add --type ...`)
- Error workaround or non-obvious pattern -> **learning** (`scribe/notes.py learn`)

---

## List-form subprocess for long CLI arguments

When invoking a CLI tool with a text argument longer than ~3 lines or containing markdown, **always** use list-form subprocess -- never bash with shell quoting (backticks trigger command substitution, apostrophes break quoting).

```python
subprocess.run(["python", "scribe/notes.py", "add", "--type", "handoff",
                 "--content", long_text_var], ...)
```

**Never:** `python scribe/notes.py add --content "text with `backticks` and it's broken"`

---

## Historical drift

Old references are archival -- do not re-introduce removed names.

- **Clarifier** -- removed 2026-04-07. The `clarifier/` directory and `/clarifier` command no longer exist.
- **Pipeline -> Runner** -- renamed 2026-04-07. The orchestrator lives at `runner/` (formerly `pipeline/`).
