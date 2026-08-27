---
type: standard
title: Code Style
scope: project
description: Naming, structure, imports, error handling, and testing conventions for Python code
framework_version: "1.0"
last_verified: 2026-08-26
---

# Code Style

Conventions derived from the existing codebase. Follow these when writing or modifying Python code.

## General

- **Python 3.11+** — use modern syntax (type hints, `pathlib`, f-strings, union `X | Y`, match statements) but stay within 3.11 compatibility. CI runs 3.11 and 3.12 on ubuntu, windows and macos.
- **Stdlib only at runtime** — no third-party import in any module a hook, a git hook or a slash command can reach. Those run under `py -3` / `python3`, not the Poetry virtualenv, so a third-party import there is a crash, not a dependency. The exceptions are declared and quarantined in `pyproject.toml`: `pytest`/`pytest-cov` in the `dev` group (tests only), and `pywebview`/`pywinpty`/`pythonnet`/`watchdog` in the optional `gui` group, which nothing outside `gui/` imports.
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
| Private functions | `_leading_underscore` | `_file_lock()`, `_flag_path()` |
| CLI subcommands | `kebab-case` (argparse) | `archive-learning`, `backfill-brief` |

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
- **Write unittest-style test classes; run them with pytest.** `unittest.TestCase` subclasses and `self.assert*` are the house style and every one of the ~100 test files follows it. The canonical runner is nonetheless pytest (`poetry run pytest -q`, configured in `pyproject.toml`), which discovers and runs unittest classes natively. Do not use pytest-only APIs — bare `assert` in a module-level function, `@pytest.fixture`, `@pytest.mark.parametrize`, `pytest.raises` — so every file also stays runnable as `python -m unittest <path>`.
- Test file naming: `test_<module>.py`.
- Each test method tests one behavior. Name it `test_<what_it_tests>`.
- Use `tempfile.TemporaryDirectory()` for tests that write files — never touch real user data.
- **Use `core/testing.py` for a git repo or a fake main-apiary.** `init_git_repo(path)` and `make_fake_apiary(root, …)` are the shared fixtures; rolling your own `git init` + `copytree` in a `setUp` is what made the install suite take a minute. `hermetic_env(**overrides)` is the env to hand a subprocess under test — never `os.environ.copy()`, which carries a live session's `CLAUDE_PROJECT_DIR` and `APIARY_*` into the thing being tested.
- Coverage is report-only and off by default: `poetry run pytest --cov` when you want the numbers. Do not add `--cov` to `addopts` and do not gate on a percentage.
- Integration tests that depend on real data should be isolated (see `test_hooks.py` for pattern).
- **Runner-package import convention.** `runner/` is a proper Python package. All runner modules use relative imports for siblings (e.g. `from .config_loader import get as cfg`). Tests use absolute package imports (e.g. `from runner import detached_lib` or `from runner.executor import execute_step`). Entry points are invoked as `python -m runner.X` from the repo root, never as `python runner/X.py`. Mock patches must use the full module path (e.g. `mock.patch('runner.run.log_stage_cost')`, not `mock.patch('run.log_stage_cost')`).

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
| Hook context formatting | `core/hook_context.py` — `context_block()`, `join_contexts()`, `read_payload()` |
| Hook registration | `core/hooks_lib.py` — `register_hooks()`, `remove_hooks()` |
| Session identity | `core/session.py` — `SessionId` class |
| File locking | `core/utils/filelock.py` — `FileLock` context manager |
| Repo root from a path | `core/utils/gitutil.py` — `git_root()`, `main_worktree_root()` |
| Reading a JSON object tolerantly | `core/utils/jsonio.py` — `read_json_object()` |
| Writing a file without a torn read | `core/utils/atomic.py` — `write_text_atomic()`, `write_json_atomic()` |
| A UTC timestamp | `core/utils/timeutil.py` — `now_iso()` |
| State dirs, the registry, version pins | `core/utils/state.py` — `resolve_state_dir()`, `read_apiary_version()`, … |
| JSONC (comments, trailing commas) | `core/utils/jsonc.py` — `load()`, `loads()` |
| Test fixtures (git repo, fake apiary, env) | `core/testing.py` — `init_git_repo()`, `make_fake_apiary()`, `hermetic_env()` |

This rule failed on its own for two years — the review found the same
`git rev-parse` block in eight files. `scripts/check_duplicates.py` is the
backstop: it reports identical and high-overlap function bodies across the
tree, and CI runs it on every push. When it names a pair you wrote, either
reuse the original or say in the code why the copy has to exist.
