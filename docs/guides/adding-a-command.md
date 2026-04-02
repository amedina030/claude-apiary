---
type: guide
title: Adding a Command
scope: project
description: End-to-end steps for writing a slash command, registering it, and testing
framework_version: "1.0"
last_verified: 2026-04-02
---

# Adding a Command

How to add a new slash command to the project.

## Prerequisites

- Slash commands are markdown files that Claude Code loads at session start
- They define instructions for Claude, not executable code
- See existing commands in `*/commands/*.md` for examples

## Steps

### 1. Create the command file

Place it at `<tool>/commands/<name>.md`. Use this structure:

```markdown
---
name: <command-name>
description: Brief description
user-invocable: true
---

Instructions for Claude when this command is invoked.

Include:
- What Claude should do
- Any arguments the command accepts
- Scripts to run, if applicable
```

Key rules:
- `name` must match the filename (without `.md`)
- `user-invocable: true` makes it appear in `/help` and tab completion
- Keep instructions precise — Claude follows them literally

### 2. Register in setup.py

Add the command file to the list of files `setup.py` copies to `~/.claude/commands/`. The copy logic is in the global install section.

### 3. Test

- Run `python setup.py --global` to install
- Start a new Claude Code session
- Type `/<name>` and verify it works as expected
- Run `python setup.py --check` to verify installation

### 4. Update docs

- Add the command to `docs/reference/slash-commands.md`
- If the command uses a CLI script, ensure it's in `docs/reference/cli-tools.md`

## Checklist

- [ ] Command markdown file created under `<tool>/commands/`
- [ ] Frontmatter includes `name`, `description`, `user-invocable`
- [ ] Added to `setup.py` copy list
- [ ] `docs/reference/slash-commands.md` updated
- [ ] `python setup.py --check` passes
- [ ] `python docs/check.py` passes
