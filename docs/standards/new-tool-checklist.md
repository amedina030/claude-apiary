---
type: standard
title: New Tool Checklist
scope: project
description: What a new tool needs — directory structure, hooks, commands, tests, docs, and install integration
framework_version: "1.0"
last_verified: 2026-08-26
---

# New Tool Checklist

When adding a new top-level tool to the project (like budgeter, scribe, or harden), every item below must be addressed.

## Directory structure

```
<tool>/
├── hooks/               # If the tool uses hooks
│   ├── pre_tool_use.py
│   └── ...
├── commands/            # Slash command definitions
│   └── <command>.md
├── lib/                 # Internal library modules (if needed)
│   └── ...
├── config.json          # Default config (if configurable)
├── <main_script>.py     # CLI entry point(s)
└── test_<module>.py     # Tests
```

## Required pieces

### 1. Core functionality
- [ ] Main Python module(s) under `<tool>/`
- [ ] Use `core/` utilities — don't reinvent flags, config, file locking, or hook registration
- [ ] Stdlib only — no external dependencies

### 2. Hooks (if applicable)
- [ ] Hook scripts under `<tool>/hooks/`
- [ ] Each hook has a silent try/except wrapper — hooks must not crash
- [ ] Registered in `core/hooks_factory.py` via `core/hooks_lib.py`

### 3. Slash commands
- [ ] Command markdown files under `<tool>/commands/`
- [ ] Each command file defines: name, description, what it does, any arguments
- [ ] Copied to `<repo>/.claude/commands/` by `apiary install`

### 4. Configuration (if applicable)
- [ ] Default config at `<tool>/config.json`
- [ ] Loaded with a stdlib `json` read and an in-module defaults fallback
- [ ] Per-project override support if needed (`.claude/<tool>.json`)

### 5. Tests
- [ ] Test file: `<tool>/test_<module>.py`
- [ ] Uses `unittest` and `tempfile.TemporaryDirectory()`
- [ ] Isolated from real user data
- [ ] Covers core logic and edge cases

### 6. Install integration
- [ ] `core/hooks_factory.py` registers hooks (if any) in `settings.json`
- [ ] `apiary install` copies commands to `<repo>/.claude/commands/`
- [ ] `poetry run apiary doctor` validates the new tool's installation
- [ ] Uninstall instructions added to `SETUP.md`

### 7. Documentation
- [ ] Update `docs/reference/cli-tools.md` with new CLI entry points
- [ ] Update `docs/reference/slash-commands.md` with new commands
- [ ] Update `docs/reference/hooks.md` if new hooks added
- [ ] Update `docs/reference/config-files.md` if new config added
- [ ] Update `docs/reference/file-storage.md` if new runtime data locations
- [ ] Update `docs/_index.md` if new docs created
- [ ] Update the `README.md` tool listing
- [ ] Run `python docs/check.py` to verify conformance

### 8. CLAUDE.md rules (if needed)
- [ ] Add the rule under `context-rules/<category>/<id>.md`
- [ ] `apiary install` renders it into the managed zone of `<repo>/CLAUDE.md`
