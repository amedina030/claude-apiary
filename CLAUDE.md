# claude-apiary

A unified ecosystem of tools that extend Claude Code: **budgeter**, **scribe**, **refiner**, **harden**, **runner**, and shared **core** infrastructure. See `README.md` for the full overview of what each tool does.

---

## Session behavior

### CLI tool lookup is mandatory

Before calling any repo CLI tool for the first time in a session, look up its full flags:

```bash
python docs/reference/cli_lookup.py <tool>   # e.g. notes, report, round_counter
```

The `enforce_cli_lookup` PreToolUse hook blocks first invocations that skip this. **When in doubt later in the session, look it up again** — LLM muscle memory for flag names (`--limit` vs `--last`, `--max` vs `--count`, etc.) is not reliable and the hook only re-blocks the first call per session.

### Handoff backfilling

A `[startup]` context block is injected on the first user message. A PreToolUse hook later injects `unseen_sessions: <list>` on the first tool call.

- **Auto-startup ON** (default — `~/.claude/auto-startup-enabled` present): if `unseen_sessions` is anything other than `none`, run `/backfill-handoffs` to process them into handoff notes. If it says `none`, skip it entirely.
- **Auto-startup OFF**: unseen detection is skipped; the user runs `/backfill-handoffs` manually when they want it.

When the backfill transcript agent's task notification arrives, pipe its `<usage>` block to the cost logger silently (do not mention to the user):

```bash
echo '<usage>...</usage>' | python $CLAUDE_PROJECT_DIR/budgeter/log_agent_cost.py --session-id "<full_session_id>" --agent "backfill-handoffs" --cwd "$CLAUDE_PROJECT_DIR"
```

### Auto-memory path correction

The session turn-0 context injected by the harness may reference a memory directory derived from `cwd` (e.g. `C:\Users\...\projects\D--Professional-claude-apiary\memory\`). The **actual** memory directory for this repo, after the T5c stable-project-key migration, is:

```
~/.claude/projects/claude-apiary/memory/
```

Always use the stable-key path above when reading or writing memory files. Do not write to the cwd-derived path — it is a stale leftover and the harness will not index what you put there.

---

## Portability

All code in this repo — hooks, scripts, `settings.json` entries, Python modules — must be portable across Windows / macOS / Linux without OS-specific branches. **Read `PORTABILITY.md` at the repo root before writing or modifying any of these.**

Summary rules (see `PORTABILITY.md` for the canonical list):

- **No absolute paths.** Use `$CLAUDE_PROJECT_DIR` in `settings.json` hook commands, `pathlib.Path(__file__).resolve().parent` in Python, `Path.home()` for user home.
- **No `python.exe` or `.exe` suffixes.** Use `python` on PATH or `sys.executable`.
- **Subprocess:** list-form (`["git", "status"]`), never `shell=True`.
- **Paths:** `pathlib.Path` end-to-end. Never concatenate with `/` or `\` literals.
- **Null device:** `os.devnull` / `subprocess.DEVNULL`, never literal `NUL` or `/dev/null`.
- **File I/O:** always explicit `encoding="utf-8"`.
- **Persistent state:** lives under `~/.claude/projects/claude-apiary/` (stable key, not cwd-derived).

If you spot a portability violation while doing unrelated work, add it to the backlog rather than fixing it inline — the user wants the portability sweep to land in coherent passes.

---

## Notes, learnings, and memory

This repo uses **scribe** (`scribe/notes.py`) for operational state (TODOs, handoffs, decisions, blockers, wishlists) and a **memory** directory for permanent facts (user preferences, project structure, reference patterns).

**Read `scribe/CLAUDE.md` before writing notes, learnings, or making memory decisions.** It has the detailed rules for when to write, when not to write, and how to differentiate notes/memory/learnings.

Quick decision tree:

- Still true in 3 months → **memory** (`~/.claude/projects/claude-apiary/memory/`)
- Decays / operational / about current work → **note** (`scribe/notes.py add --type ...`)
- Error workaround or non-obvious pattern discovered during task → **learning** (`scribe/notes.py learn`)

---

## Git commits

The "no Co-Authored-By Claude" rule is now a context-rule shipped via apiary's
context-rules system. Install it (along with the other behavioral rules) into
your global `~/.claude/CLAUDE.md` with:

```bash
python scripts/install_context_rules.py --install-all
```

Source: `context-rules/behavioral/no_coauthored_by.md`. The bootstrap script
prompts to install all context-rules on first run.

---

## Historical drift

Some pieces of the codebase have been renamed or removed during cleanup passes. Historical notes, transcripts, and backlog tickets may reference them. Treat these references as archival — do not re-introduce the old names.

- **Clarifier** — removed 2026-04-07. The `clarifier/` directory, `/clarifier` command, and the clarifier sub-agent no longer exist.
- **Pipeline → Runner** — renamed 2026-04-07. The autonomous orchestrator lives at `runner/` (formerly `pipeline/`). The `runner/<uuid>` git branch prefix and all docs/code reflect the new name. The old `pipeline/<uuid>` branch naming convention is gone for new runs, though in-flight or merged historical branches may still carry it.
