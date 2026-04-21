# Setup Guide

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/claude-code) installed and configured
- [Poetry](https://python-poetry.org/) (recommended) or pip

No external runtime dependencies — dev dependencies (pytest) are managed via `pyproject.toml`.

---

## New Machine Install

### 1. Clone the repo

```bash
git clone https://github.com/amedina030/claude-apiary.git /path/to/claude-apiary
cd /path/to/claude-apiary
```

### 2. Install dependencies

```bash
# Preferred: Poetry (creates a virtualenv, locks dependencies)
poetry install

# Alternative: pip (no lockfile)
pip install -r requirements.txt
```

### 3. Run the installer

```bash
python setup.py --global
```

This will:
- Register all hooks (budgeter, transcript saver, install checker) in `~/.claude/settings.json`
- Copy slash commands to `~/.claude/commands/` (budgeter, scribe, startup, etc.)
- Check whether `~/.claude/CLAUDE.md` exists

### 4. Start a new Claude Code session

Hooks and command files are loaded at session start. Restart Claude Code after setup.

### 5. Enable the features you want

In any Claude Code session:

```
/budgeter-log     # start recording token usage
/budgeter-warn    # enable expensive-call warnings
```

Scribe (notes, learnings, handoffs) and the `/startup` skill are always active — no toggle needed.

Each toggle persists across sessions (stored as a flag file in `~/.claude/`).

---

## Per-Project Install (budgeter only)

To scope budgeter logging to a single project instead of globally:

```bash
python setup.py --project-path /path/to/your/project
```

This creates `.claude/budgeter.json` inside the project with independent config and stores logs at `.claude/budgeter-log.jsonl`.

---

## Re-running Setup

Safe to re-run at any time — old `claude-apiary` hook entries are stripped before writing new ones, so no duplicates accumulate. Run it again after pulling updates to keep installed files in sync.

---

## Updating

```bash
git pull
poetry install    # or: pip install -r requirements.txt
python setup.py --global
```

---

## Uninstalling

**Hook entries (all tools):**

```bash
python scripts/uninstall_hooks.py --list        # see what's installed
python scripts/uninstall_hooks.py --dry-run     # preview what would be removed
python scripts/uninstall_hooks.py --uninstall   # remove apiary entries from ~/.claude/settings.json
```

The script removes every apiary-owned hook entry — budgeter, core, scribe, docs, refiner, harden, runner — in one pass. It uses `core.hooks_lib.is_apiary_entry` so portable `$CLAUDE_PROJECT_DIR`-form entries and absolute-path entries are both recognized; unrelated third-party entries are never touched. Pass `--settings <path>` to target a project-local settings file.

**Context-rules zone in `~/.claude/CLAUDE.md`:**
```bash
python scripts/install_context_rules.py --uninstall
```

**Budgeter:**
- Optionally delete `~/.claude/budgeter-log-enabled` and `~/.claude/budgeter-warn-enabled`

**Scribe:**
- Delete `~/.claude/commands/note.md`, `~/.claude/commands/notes.md`, `~/.claude/commands/startup.md`
- Optionally delete the repo-local scribe state at `<repo-root>/.apiary/scribe/` (new canonical location under the umbrella `.apiary/` dir). If the repo still has pre-migration state at `~/.claude/projects/*/notes.jsonl`, `learnings.jsonl`, and `notes_archive.jsonl`, delete those too.

**Docs framework:**
- Delete `~/.claude/commands/review.md`
- Delete `.git/hooks/pre-commit` (if it references `docs/check.py`)

**Researcher:**
- Delete `~/.claude/commands/research.md`
- Optionally delete the repo-local researcher state at `<repo-root>/.apiary/research/` (entries and `tags.yaml`)

**Runner:**
- Delete `~/.claude/commands/runner-prep.md`
- Runner state (intake, backlog, plans, hardens, executions, reports, logs) lives inside the apiary repo under `runner/` — it is removed when the repo directory is deleted

**Everything:**
- Delete the `claude-apiary` repo directory

---

## Troubleshooting

**Hooks not firing**
Start a new Claude Code session — hooks are loaded at session start, not mid-session.

**`budgeter-log` toggle has no effect**
Check that `~/.claude/budgeter-log-enabled` exists (ON) or is absent (OFF). The slash command creates/removes this file.

**Warnings never firing**
Warnings require at least `min_tasks` unique tasks in the log (default: 50). Run `/budgeter-log` to start building history, then check again after enough sessions.
