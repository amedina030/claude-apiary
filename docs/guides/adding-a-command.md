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

### 2. Register in install.py

Add the command's source dir name to `_slash_command_sources()` in `core/install.py` if it's not already covered by one of the known tool dirs. New commands placed under an existing `<tool>/commands/` are picked up automatically.

### 3. Test

- Run `poetry run apiary install --target <repo>` to propagate the command to a bootstrapped repo
- Start a new Claude Code session in that repo
- Type `/<name>` and verify it works as expected
- Run `poetry run apiary doctor registry` to verify the install hash records updated

### 4. Update docs

- Add the command to `docs/reference/slash-commands.md`
- If the command uses a CLI script, ensure it's in `docs/reference/cli-tools.md`

## Checklist

- [ ] Command markdown file created under `<tool>/commands/`
- [ ] Frontmatter includes `name`, `description`, `user-invocable`
- [ ] Tool directory is in `core/install._slash_command_sources`' tool list
- [ ] `docs/reference/slash-commands.md` updated
- [ ] `poetry run apiary doctor` passes
- [ ] `python docs/check.py` passes
