---
type: guide
title: Adding a Tool
scope: project
description: End-to-end steps for creating a new top-level tool from scratch
framework_version: "1.0"
last_verified: 2026-08-26
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
- Read it with `json.loads(path.read_text(encoding="utf-8"))` and fall back to
  the module's own defaults when the file is missing or malformed (see
  `budgeter/lib/logger.py load_config` / `runner/config_loader.py`)
- Optional per-project override at `.claude/<tool>.json`

### 6. Write tests

- `<tool>/test_<module>.py` using `unittest`
- Isolate from real user data with `tempfile.TemporaryDirectory()`
- Cover core logic and edge cases

### 7. Integrate with `apiary install`

- Register the new tool's hooks in `_registry()` in `core/hooks/dispatch.py` — one `Hook(name, module, matcher)` row per hook, in the event's tuple. `core/hooks_factory.py` registers only the dispatcher itself, once per event, so a new hook needs no `settings.json` change and no re-bootstrap.
- Slash commands placed under `<tool>/commands/*.md` are auto-discovered by `core/install._slash_command_sources`.
- Add a doctor check in `core/doctor.py` if the tool introduces new on-disk state worth validating.
- Add uninstall instructions to `SETUP.md`.

### 8. Write documentation

- Update all relevant reference docs (see [New Tool Checklist](../standards/new-tool-checklist.md) section 7)
- Update the `README.md` tool listing
- Update `docs/_index.md`

### 9. Add CLAUDE.md rules (if needed)

If the tool requires behavioral rules:
- Write them as a rule file under `context-rules/<category>/<id>.md`
- `apiary install` renders them into the managed zone of `<repo>/CLAUDE.md`

## Checklist

See [New Tool Checklist](../standards/new-tool-checklist.md) for the complete checklist.
