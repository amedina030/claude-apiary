---
type: standard
title: Code Style
scope: project
description: Naming, structure, imports, error handling, and testing conventions for Python code
framework_version: "1.0"
last_verified: 2026-04-02
---

# Code Style

Conventions derived from the existing codebase. Follow these when writing or modifying Python code.

## General

- **Python 3.9+** — use modern syntax (type hints, `pathlib`, f-strings) but stay within 3.9 compatibility.
- **Stdlib only** — no external dependencies. This is a hard rule.
- **UTF-8 everywhere** — always pass `encoding="utf-8"` to `open()`, `read_text()`, `write_text()`.

## File structure

Each Python file follows this order:

1. Shebang (if executable): `#!/usr/bin/env python3`
2. Module docstring — brief, explains what the file does
3. Stdlib imports (grouped)
4. `sys.path.insert` for repo root access (hooks only)
5. Internal imports (`from core import flags`, `from budgeter.lib import logger`)
6. Constants and module-level setup
7. Functions / classes
8. `if __name__ == "__main__": main()` guard

## Naming

| Thing | Convention | Example |
|-------|-----------|---------|
| Files | `snake_case.py` | `pre_tool_use.py`, `hook_context.py` |
| Functions | `snake_case` | `load_config()`, `is_enabled()` |
| Constants | `UPPER_SNAKE` | `CLAUDE_DIR`, `VALID_TYPES` |
| Classes | `PascalCase` | `SessionId`, `FileLock` |
| Private functions | `_leading_underscore` | `_strip_cont()`, `_parse_usage()` |
| CLI subcommands | `kebab-case` (argparse) | `handoff-sessions` |

## Imports

- Group: stdlib first, then internal. No blank line needed within a group.
- Use `from module import name` for specific items. Use `import module` for broader access.
- Hooks that need repo root access use: `sys.path.insert(0, str(Path(__file__).parent.parent.parent))`

## Error handling

- **Hooks must not crash.** Wrap the entire `main()` in a try/except that exits silently on error. A broken hook should not block the user.
- **CLI tools** should let exceptions propagate with clear error messages. Use `argparse` for input validation.
- Use `sys.exit(1)` for expected failures (bad input, missing files). Let unexpected exceptions raise naturally.

## Functions

- Keep functions short and focused. If a function does two things, split it.
- Use early returns to avoid deep nesting.
- Type hints on public functions. Optional on private helpers.
- Docstrings on public functions — one line if simple, multi-line if complex.

## File I/O

- Use `pathlib.Path` for all path operations, not `os.path`.
- Use `core/utils/filelock.py` (`FileLock`) for concurrent JSONL writes.
- Create parent directories with `path.parent.mkdir(parents=True, exist_ok=True)` before writing.

## Testing

- Tests live alongside the code they test: `budgeter/test_hooks.py`, `scribe/test_notes.py`.
- Use `unittest` (stdlib). No pytest.
- Test file naming: `test_<module>.py`.
- Each test method tests one behavior. Name it `test_<what_it_tests>`.
- Use `tempfile.TemporaryDirectory()` for tests that write files — never touch real user data.
- Integration tests that depend on real data should be isolated (see `test_hooks.py` for pattern).

## CLI patterns

- Use `argparse` with subcommands for multi-verb tools.
- Use mutually exclusive groups for mode flags (e.g. `--global | --project-path | --check`).
- Print to stdout for normal output, stderr for errors.
- Exit 0 on success, 1 on expected failure.

## Reuse core/

Before writing utility code, check if `core/` already has it:

| Need | Use |
|------|-----|
| Feature toggles | `core/flags.py` — `is_enabled()`, `enable()`, `disable()`, `toggle()` |
| JSON config loading | `core/config.py` — `load_config()`, `write_config()` |
| Hook context formatting | `core/hook_context.py` — `context_block()`, `join_contexts()`, `read_payload()` |
| Hook registration | `core/hooks_lib.py` — `register_hooks()`, `remove_hooks()` |
| Session identity | `core/session.py` — `SessionId` class |
| File locking | `core/utils/filelock.py` — `FileLock` context manager |
