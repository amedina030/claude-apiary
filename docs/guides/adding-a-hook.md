---
type: guide
title: Adding a Hook
scope: project
description: End-to-end steps for writing a new hook, registering it in the dispatcher, and testing
framework_version: "1.0"
last_verified: 2026-08-26
---

# Adding a Hook

How to add a new Python hook to the project.

Hooks are no longer separate `settings.json` entries. Every event runs one command — `core/hooks/dispatch.py <verb>` — which runs the hook modules in-process. Adding a hook therefore means adding a `run(payload)` function and one registry row; it does **not** mean re-bootstrapping every repo.

## Prerequisites

- Understand the hook lifecycle events: PreToolUse, PostToolUse, Stop, UserPromptSubmit (see [Hooks](../reference/hooks.md))
- Know which tool the hook belongs to (budgeter, core, docs, or a new tool)

## Steps

### 1. Create the hook module

Place it under `<tool>/hooks/<name>.py`. The whole contract is one function:

```python
#!/usr/bin/env python3
"""Brief description of what this hook does."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # repo root

from core.hook_context import HookResult, context_block, run_standalone


def run(payload: dict) -> HookResult | None:
    """Return what this hook has to say about *payload*, or None."""
    if payload.get("tool_name") != "Bash":
        return None
    return HookResult(context=context_block("mytool", "something worth knowing"))


if __name__ == "__main__":
    run_standalone(run)          # keeps `python <hook>.py` working
```

Key rules:

- **Return, don't print.** `None` means "no opinion" — the dispatcher merges every hook's context into one response. Printing your own JSON would corrupt it.
- **Don't catch your own exceptions.** The dispatcher wraps each hook and logs failures to `<repo>/.claude/apiary/hooks.log`. A blanket `except: pass` just hides the bug the log exists to surface.
- **Never vote on permissions.** There is no way to express `allow` in `HookResult`, and that is deliberate (review C-1). A gate that genuinely means to stop a call returns `HookResult(block_reason="...")`.
- Use `context_block()` / `join_contexts()` from `core/hook_context.py` to format context.
- Keep imports inside `run()` when they are expensive — every import is paid on every tool call.
- Pass the right event to `run_standalone` for non-PreToolUse hooks (`run_standalone(run, event="PostToolUse")`).

### 2. Register in the dispatcher

Add one row to the right event's tuple in `_registry()` in `core/hooks/dispatch.py`:

```python
Hook("my_hook", "mytool.hooks.my_hook", "Edit|Write"),
```

- *name* — what appears in `hooks.log`.
- *module* — dotted import path, or a repo-relative `.py` path for a module outside a package.
- *matcher* — regex fullmatched against `tool_name`; `None`/`""` means every tool. A non-matching hook is never imported, so a precise matcher is a real saving.

Position matters: hooks run top to bottom, and a `block_reason` stops the chain.

### 3. Test

- Write tests in `<tool>/test_<module>.py`
- Test `run(payload)` directly — it is a pure-ish function of a dict; no stdin, no subprocess
- Use `tempfile.TemporaryDirectory()` for any file I/O
- `core/hooks/test_dispatch.py` already asserts every registered module resolves and exposes `run` — a missing registration fails there
- Run `poetry run apiary doctor` to verify the install is intact

### 4. Update docs

- Add the hook to `docs/reference/hooks.md` (table **and** the execution-order list)
- If it creates new runtime files, update `docs/reference/file-storage.md`

## Checklist

- [ ] Hook module created under `<tool>/hooks/` with a `run(payload)` function
- [ ] `run_standalone(run, ...)` shim under `if __name__ == "__main__"`
- [ ] Registered in `_registry()` in `core/hooks/dispatch.py`, in the right position, with a matcher
- [ ] Tests written and passing
- [ ] `docs/reference/hooks.md` updated (table + order list)
- [ ] `poetry run apiary doctor` passes
- [ ] `python docs/check.py` passes
