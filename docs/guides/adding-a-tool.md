---
type: guide
title: Adding a Tool
scope: project
description: End-to-end steps for creating a new top-level tool from scratch
framework_version: "1.0"
last_verified: 2026-04-07
---

# Adding a Tool

How to add a new top-level tool to the project (like budgeter, scribe, or harden).

This is the most involved task — see [New Tool Checklist](../standards/new-tool-checklist.md) for the full requirements list. This guide walks through the steps in order.

## Steps

### 1. Create directory structure

```bash
mkdir -p <tool>/hooks <tool>/commands <tool>/lib
```

### 2. Write core functionality

- Main Python module(s) under `<tool>/`
- Use `core/` utilities for flags, config, file locking, hook registration
- Stdlib only — no external dependencies
- See [Code Style](../standards/code-style.md) for conventions

### 3. Add hooks (if applicable)

See [Adding a Hook](adding-a-hook.md).

### 4. Add slash commands

See [Adding a Command](adding-a-command.md).

### 5. Add configuration (if applicable)

- Create `<tool>/config.json` with sensible defaults
- Load via `core/config.py` with `load_config(path, defaults)`
- Optional per-project override at `.claude/<tool>.json`

### 6. Write tests

- `<tool>/test_<module>.py` using `unittest`
- Isolate from real user data with `tempfile.TemporaryDirectory()`
- Cover core logic and edge cases

### 7. Integrate with `apiary install`

- Add the new tool's `hooks/` builder to `core/hooks_factory.py` (extend an existing builder if the hooks fit a current event set, or add a new `build_<tool>_hooks` and wire it into `core/install.py`).
- Slash commands placed under `<tool>/commands/*.md` are auto-discovered by `core/install._slash_command_sources`.
- Add a doctor check in `core/doctor.py` if the tool introduces new on-disk state worth validating.
- Add uninstall instructions to `SETUP.md`.

### 8. Write documentation

- Update all relevant reference docs (see [New Tool Checklist](../standards/new-tool-checklist.md) section 7)
- Update `README.md` tool listing and repository structure
- Update `docs/_index.md`

### 9. Add CLAUDE.md rules (if needed)

If the tool requires behavioral rules:
- Write them in `<tool>/CLAUDE.md`
- Document that users append them to `~/.claude/CLAUDE.md`
- Add a setup.py warning if rules are missing

## Checklist

See [New Tool Checklist](../standards/new-tool-checklist.md) for the complete checklist.
