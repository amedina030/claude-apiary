---
type: guide
title: Bootstrapping a Repo
scope: project
description: Apply an apiary profile to a new or existing repo's .claude/settings.json, and how to author a new profile
framework_version: "1.0"
last_verified: 2026-04-23
---

# Bootstrapping a Repo

Apiary ships a profile-based bootstrap that writes a target repo's `.claude/settings.json` from a JSONC profile manifest. One command per target, re-runnable with drift detection.

## When to use this

- Onboarding a new repo to apiary-managed Claude Code configuration
- Updating an existing repo after a profile changes
- Migrating a hand-maintained `.claude/settings.json` into a profile

## Quick start

From the target repo:

```bash
python ~/.claude/apiary_bootstrap.py --profile <name>
```

The installer at `~/.claude/apiary_bootstrap.py` finds the apiary repo via `~/.claude/apiary.json` (written by `setup.py --global`). It resolves `<apiary-repo>/profiles/<name>.jsonc`, walks its `extends` chain, and writes two files:

| File | Purpose |
|------|---------|
| `.claude/settings.json` | Claude Code config — apiary-owned top-level keys replaced, non-apiary keys preserved |
| `.apiary/bootstrap_state.json` | Provenance + drift-detection state (see [File Storage](../reference/file-storage.md#bootstrap-state)) |

## Profile manifest format

Profiles live at `<apiary-repo>/profiles/<name>.jsonc`. JSONC (JSON + `//` and `/* */` comments + trailing commas) so manifests can explain themselves.

```jsonc
// profiles/unreal.jsonc
{
  "$schema_version": 1,
  "extends": ["base"],
  "permissions": {
    "allow": ["Bash(ue-editor --headless *)"]
  },
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "mcp__ue_llm_toolkit__.*",
        "hooks": [
          {"type": "command", "command": "python ~/.claude/apiary_launch.py observers/ue_llm_toolkit.py"}
        ]
      }
    ]
  }
}
```

### Required fields

| Field | Type | Description |
|------|------|-------------|
| `$schema_version` | int | Must be `1` for the current loader |

### Optional fields

| Field | Type | Description |
|------|------|-------------|
| `extends` | list of strings | Parent profile names merged left-to-right before this profile |
| anything else | any | Carried through verbatim and merged into `.claude/settings.json` |

## Merge semantics

- **Profiles stack via `extends`.** Parents merge first, then the child on top. Chains are walked depth-first with cycle detection — `ProfileCycleError` on loops.
- **Dicts recurse.** Nested objects merge per-key.
- **Lists concatenate.** Parent entries appear before child entries.
- **Scalars — last wins.** Child overrides parent.
- **`{"$replace": <value>}`** at any position replaces instead of merging. Use it when you want a clean slate for a list or object that a parent already populated.

Between the resolved profile and the target repo's existing `.claude/settings.json`: **apiary-owned top-level keys are fully replaced**, non-apiary top-level keys pass through verbatim. Replace (not merge) at the top-level boundary keeps re-runs idempotent — a re-run with an unchanged profile is a content-level no-op.

If you want to extend an apiary-managed key (e.g. add a custom permission alongside the profile's), author a local profile that extends the apiary one rather than hand-editing the managed file.

## Adding a new profile

1. Create `<apiary-repo>/profiles/<your-profile>.jsonc` with `$schema_version: 1`, an optional `extends`, and the keys you want to manage.
2. Run the bootstrap against a test repo to verify the merge:
   ```bash
   cd /tmp/test-repo
   python ~/.claude/apiary_bootstrap.py --profile <your-profile>
   cat .claude/settings.json
   ```
3. Commit `<apiary-repo>/profiles/<your-profile>.jsonc` to apiary.

The ship-set profiles are:

| Profile | Extends | Scope |
|---------|---------|-------|
| `base.jsonc` | — | Minimum permissions every apiary-managed repo needs (scribe CLI, transcript hook, session identity) |
| `apiary.jsonc` | `base` | Apiary's self-dogfood — adds the dogfood + ue_llm_toolkit observer hooks |
| `unreal.jsonc` | `base` | Unreal Engine projects — adds the ue_llm_toolkit observer hook only |

## Re-run drift

On any run after the first, the bootstrap:

1. Loads `.apiary/bootstrap_state.json`.
2. Computes the new merge.
3. If the new merge equals the current `.claude/settings.json`, quietly re-writes state and exits 0.
4. If it differs, prints a per-key before/after diff and prompts `y/N`.
5. `--force` skips the prompt.
6. Non-TTY stdin without `--force` is a hard error (exit 1) — safer than hanging or silently applying.

## Error cases

| Situation | Exit code | Source |
|-----------|-----------|--------|
| Profile name not found | 2 | Prints the list of profiles available under `<apiary-repo>/profiles/` |
| `extends` cycle | 2 | Prints the cycle path (`a -> b -> a`) |
| `$schema_version` missing or unsupported | 2 | Asks the user to upgrade apiary |
| JSONC parse error | 2 | Carries the file path + line number |
| Observer script referenced by a hook command but missing from `observers/` | 0 | Warns on stderr; the hook entry is written anyway (AC-9) |
| Aborted at drift prompt | 1 | State and settings unchanged |
| Non-TTY re-run without `--force` | 1 | Says "re-run with --force to apply non-interactively" |

## Pre-existing `.apiary/` directories

If the target already has `.apiary/scribe/` or other subsystems in place but no `bootstrap_state.json`, the bootstrap treats the run as fresh. Existing scribe, research, compass, and observer state are left untouched (AC-21).

## Related

- [File Storage — Bootstrap state](../reference/file-storage.md#bootstrap-state)
- [CLI Tools — `core/apiary_bootstrap.py`](../reference/cli-tools.md#coreapiary_bootstrappy)
- `core/apiary_profiles.py` — profile loader + deep merge + `$replace` implementation
- `core/utils/jsonc.py` — JSONC parser (stdlib-only comment stripper)
