# Setup Guide

## Requirements

- Python 3.9+
- [Claude Code](https://claude.ai/claude-code) installed and configured

No external Python dependencies.

---

## New Machine Install

### 1. Clone the repo

```bash
git clone https://github.com/amedina030/claude-apiary.git /path/to/claude-apis
cd /path/to/claude-apis
```

### 2. Run the installer

```bash
python setup.py --global
```

This will:
- Register all hooks (budgeter, transcript saver, install checker) in `~/.claude/settings.json`
- Copy the clarifier agent to `~/.claude/agents/clarifier.md`
- Copy slash commands to `~/.claude/commands/` (budgeter, clarifier, scribe, startup)
- Check whether the clarifier and scribe rules are present in `~/.claude/CLAUDE.md`

### 3. Add clarifier rules to CLAUDE.md (if warned)

If setup prints a warning about `CLAUDE.md`, append the clarifier rules manually:

```bash
cat /path/to/claude-apis/clarifier/CLAUDE.md >> ~/.claude/CLAUDE.md
```

This only needs to be done once. The clarifier rules define when and how ambiguity detection fires — they live in `CLAUDE.md` because they govern Claude's core behavior, not a specific hook.

### 4. Start a new Claude Code session

Hooks and agent files are loaded at session start. Restart Claude Code after setup.

### 5. Enable the features you want

In any Claude Code session:

```
/budgeter-log     # start recording token usage
/budgeter-warn    # enable expensive-call warnings
/clarifier        # enable ambiguity detection
```

Scribe (notes, learnings, handoffs) and the `/startup` skill are always active — no toggle needed.

Each toggle persists across sessions (stored as a flag file in `~/.claude/`).

---

## Per-Project Install (budgeter only)

To scope budgeter logging to a single project instead of globally:

```bash
python setup.py --project-path /path/to/your/project
```

This creates `.claude/budgeter.json` inside the project with independent config and stores logs at `.claude/budgeter-log.jsonl`. The clarifier is always global — it's not project-scoped.

---

## Optional: Install Test Suite

To install the clarifier test fixtures (needed for `/run-clarifier-tests`):

```bash
python setup.py --global --with-test-suite
```

---

## Re-running Setup

Safe to re-run at any time — old `claude-apis` hook entries are stripped before writing new ones, so no duplicates accumulate. Run it again after pulling updates to keep installed files in sync.

---

## Updating

```bash
git pull
python setup.py --global
```

If `clarifier/CLAUDE.md` has changed, re-apply the updated rules to `~/.claude/CLAUDE.md` manually.

---

## Uninstalling

**Budgeter:**
- Remove the budgeter hook entries from `~/.claude/settings.json` (any entry containing `claude-apis`)
- Optionally delete `~/.claude/budgeter-log-enabled` and `~/.claude/budgeter-warn-enabled`

**Clarifier:**
- Delete `~/.claude/agents/clarifier.md`
- Delete `~/.claude/commands/run-clarifier-tests.md`

**Scribe:**
- Delete `~/.claude/commands/note.md`, `~/.claude/commands/notes.md`, `~/.claude/commands/startup.md`
- Remove the scribe/learnings sections from `~/.claude/CLAUDE.md`
- Optionally delete `~/.claude/projects/*/notes.jsonl`, `learnings.jsonl`, and `notes_archive.jsonl`

**Docs framework:**
- Delete `~/.claude/commands/review.md`
- Remove the docs reminder hook entries from `~/.claude/settings.json` (entries containing `remind_standards`)
- Delete `.git/hooks/pre-commit` (if it references `docs/check.py`)

**Everything:**
- Delete the `claude-apis` repo directory

---

## Troubleshooting

**Hooks not firing**
Start a new Claude Code session — hooks are loaded at session start, not mid-session.

**`budgeter-log` toggle has no effect**
Check that `~/.claude/budgeter-log-enabled` exists (ON) or is absent (OFF). The slash command creates/removes this file.

**Warnings never firing**
Warnings require at least `min_tasks` unique tasks in the log (default: 50). Run `/budgeter-log` to start building history, then check again after enough sessions.
