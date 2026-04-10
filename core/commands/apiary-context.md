---
name: apiary-context
description: Load apiary toolkit context (scribe, budgeter, runner, portability rules)
user-invocable: true
---

This environment uses **claude-apiary** -- a toolkit extending Claude Code with **budgeter**, **scribe**, **refiner**, **harden**, **runner**, and shared **core** infrastructure.

---

## CLI invocation — the launcher

All apiary CLI tools must be invoked via the launcher, which resolves the apiary repo path programmatically:

```bash
python ~/.claude/apiary_launch.py <relative-script-path> [args...]
```

The launcher reads `~/.claude/apiary.json`, sets cwd to the apiary repo, and forwards all arguments. This works from any directory — no `<repo_dir>` substitution needed.

Examples:
```bash
python ~/.claude/apiary_launch.py scribe/notes.py list --type todo
python ~/.claude/apiary_launch.py budgeter/report.py --since 7d
python ~/.claude/apiary_launch.py harden/round_counter.py start --session-id abc12345
```

## CLI tool lookup

When in doubt about a CLI tool's flags, look it up rather than guessing:

```bash
python ~/.claude/apiary_launch.py docs/reference/cli_lookup.py <tool>   # e.g. notes, report, round_counter
```

---

## Handoff backfilling

If `unseen_sessions` (injected by a PreToolUse hook on the first tool call) is anything other than `none`, run `/backfill-handoffs`. If `none`, skip it.

When the backfill agent's task notification arrives, pipe its `<usage>` block to the cost logger silently:

```bash
echo '<usage>...</usage>' | python ~/.claude/apiary_launch.py budgeter/log_agent_cost.py --session-id "<full_session_id>" --agent "backfill-handoffs"
```

Note: the `--cwd` flag is no longer needed — the launcher sets cwd to the apiary repo automatically.

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
- Decays / operational / about current work -> **note** (`python ~/.claude/apiary_launch.py scribe/notes.py add --type ...`)
- Error workaround or non-obvious pattern -> **learning** (`python ~/.claude/apiary_launch.py scribe/notes.py learn`)

---

## List-form subprocess for long CLI arguments

When invoking a CLI tool with a text argument longer than ~3 lines or containing markdown, **always** use list-form subprocess -- never bash with shell quoting (backticks trigger command substitution, apostrophes break quoting).

```python
subprocess.run(["python", os.path.expanduser("~/.claude/apiary_launch.py"),
                 "scribe/notes.py", "add", "--type", "handoff",
                 "--content", long_text_var], ...)
```

**Never:** `python scribe/notes.py add --content "text with `backticks` and it's broken"`

---

## Historical drift

Old references are archival -- do not re-introduce removed names.

- **Clarifier** -- removed 2026-04-07. The `clarifier/` directory and `/clarifier` command no longer exist.
- **Pipeline -> Runner** -- renamed 2026-04-07. The orchestrator lives at `runner/` (formerly `pipeline/`).
