---
type: guide
title: Adding a Hook
scope: project
description: End-to-end steps for writing a new hook, registering it in setup.py, and testing
framework_version: "1.0"
last_verified: 2026-04-07
---

# Adding a Hook

How to add a new Python hook to the project.

## Prerequisites

- Understand the hook lifecycle events: PreToolUse, PostToolUse, Stop (see [Hook Lifecycle](../architecture/hook-lifecycle.md))
- Know which tool the hook belongs to (budgeter, scribe, core, or a new tool)

## Steps

### 1. Create the hook script

Place it under `<tool>/hooks/<name>.py`. Follow the standard structure:

```python
#!/usr/bin/env python3
"""Brief description of what this hook does."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # repo root

from core.hook_context import read_payload

def main():
    try:
        payload = read_payload()
        # Hook logic here
    except Exception:
        pass  # Hooks must not crash

if __name__ == "__main__":
    main()
```

Key rules:
- Wrap `main()` in try/except — a broken hook must not block the user
- Use `read_payload()` from `core/hook_context.py` to parse stdin
- Use `context_block()` and `join_contexts()` to format output
- Print to stdout to inject context into Claude's view

### 2. Register in setup.py

Add the hook to the appropriate section in `setup.py` using `core/hooks_lib.py`:
- Specify the `matcher` (tool type or `"*"` for all)
- Specify the lifecycle event (`PreToolUse`, `PostToolUse`, or `Stop`)
- Use the full path from repo root in the command string

### 3. Test

- Write tests in `<tool>/test_<module>.py`
- Use `tempfile.TemporaryDirectory()` for any file I/O
- Test the hook's logic directly (import and call functions) — don't shell out
- Run `poetry run apiary doctor` to verify registration

### 4. Update docs

- Add the hook to `docs/reference/hooks.md`
- If it creates new runtime files, update `docs/reference/file-storage.md`

## Checklist

- [ ] Hook script created under `<tool>/hooks/`
- [ ] try/except wrapper around main logic
- [ ] Added to the appropriate builder in `core/hooks_factory.py`
- [ ] Tests written and passing
- [ ] `docs/reference/hooks.md` updated
- [ ] `poetry run apiary doctor` passes
- [ ] `python docs/check.py` passes
