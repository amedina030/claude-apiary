# Migrations

Per-repo apiary version migrations. Each file upgrades a bootstrapped repo
from one apiary version to the next. `apiary update` chains them in order.

> Spec: [`docs/architecture/per-repo-install.md`](../docs/architecture/per-repo-install.md).

## Naming

`v<from>_to_v<to>.py` — full 3-part semver, dots replaced with underscores.

| File | Migrates |
|---|---|
| `v0_0_0_to_v0_1_0.py` | 0.0.0 → 0.1.0 |
| `v0_1_0_to_v0_2_0.py` | 0.1.0 → 0.2.0 |
| `v0_3_5_to_v0_3_6.py` | 0.3.5 → 0.3.6 (patch bumps still get a file when they need migration logic) |

## Required module attributes

```python
"""Brief one-line description of what this migration does.

What changes: <specifics>
Idempotent: yes (safe to run twice).
"""
from pathlib import Path

FROM_VERSION = "0.1.0"
TO_VERSION = "0.2.0"


def upgrade(repo_path: Path) -> None:
    """Apply the migration. Must be idempotent and atomic.
    Raise on failure — caller will roll back."""
    ...
```

`apiary update` imports the module, reads `FROM_VERSION`/`TO_VERSION`, and
invokes `upgrade(repo_path)`. The `repo_path` argument is the absolute path
to the bootstrapped repo being upgraded (NOT main-apiary).

## Contract

Each migration MUST be:

- **Idempotent.** Running `upgrade()` twice on the same repo produces the
  same final state as running it once. This protects against partial-run
  retries and makes "did this already run?" a non-question.
- **Atomic.** Either the migration completes fully or leaves the repo
  exactly as it was. Practically: stage changes, then swap; or write to a
  staging dir and rename at the end. Use `core/utils/state._write_json_file`
  pattern (.tmp + os.replace) for any single-file writes.
- **Forward-only.** No `downgrade()` function. Rolling back a deployed
  apiary version is out of scope.
- **No external side effects.** Migrations touch only files inside
  `repo_path` and `<main-apiary>/.repos/<slug>/`. They do not reach out
  to the network, GUI, or other repos.

If `upgrade()` raises, `apiary update`:

1. Reverts `<repo>/.claude/apiary/version.json` to the pre-migration value
   (it had not yet been bumped to TO_VERSION — version.json is only
   updated on successful return).
2. Aborts the chain — later migrations in the chain do not run.
3. Surfaces the error to the user.

## Why we keep these in git indefinitely

Per D12: a long-dormant repo coming back online (e.g. a v0.1 repo opened
months later when main-apiary is at v0.5) must be able to chain
v0.1→v0.2→...→v0.5 using migrations that ship with the current
main-apiary checkout. Migrations are tiny and tracked — never moved to
`.scrap/` or untracked locations.

## Helpers (planned, not yet implemented)

A future `migrations/_lib.py` will host shared helpers for migrations
(snapshot-before-mutate, etc.). Still an open question.

## Phase-0 status

This directory is being created during phase 0 of the per-repo migration.
The single file currently here is `v0_0_0_to_v0_1_0.py` — a no-op example
serving as a shape reference. The first real migration will be added when
main-apiary's version is bumped to 0.2.0.
