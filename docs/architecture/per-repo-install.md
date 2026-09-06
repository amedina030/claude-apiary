---
type: architecture
title: Per-Repo Install Model
scope: project
description: How apiary is installed, where state lives, and how drift is detected after the per-repo migration (2026-05)
framework_version: "1.0"
last_verified: "2026-09-06"
---

# Per-Repo Install Model

Apiary moved from a single global install in `~/.claude/` to a fully per-repo
install in 2026-05. This doc captures the architecture — what each piece is,
where it lives, and how the parts coordinate.

## Overview

- **Main-apiary** — the apiary checkout itself. Holds the source code, the
  registry of bootstrapped repos at `<main-apiary>/.repos/registry.json`,
  per-target state under `<main-apiary>/.repos/<slug>/`, version migrations
  under `<main-apiary>/migrations/`, and GUI state under
  `<main-apiary>/.apiary/gui/`. Exactly one main-apiary per machine.
- **Bootstrapped repo** — a repo onboarded via `apiary install --target <repo>`.
  Has its own `.claude/settings.json` with hooks, its own `.claude/commands/`,
  and three small pin files at `<repo>/.claude/apiary/` identifying main-apiary
  and itself. An entry in main-apiary's registry mirrors the pin.
- **Dual role** — main-apiary is also a bootstrapped repo from its own POV.
  Its UID is reserved at slot 1 by convention.
- **Opt-in per repo** — sessions started in a non-bootstrapped repo run as
  vanilla Claude Code with no apiary hooks, no managed CLAUDE.md zone, no
  budgeter logging. Apiary is silent unless cwd is bootstrapped.

## File layout

```
~/.claude/                                       # apiary writes nothing here
  CLAUDE.md                                      # user-owned only
  projects/<project-key>/*.jsonl                 # Claude Code's transcripts (apiary reads, never writes)
  settings.json                                  # Claude Code's, no apiary hooks

<main-apiary>/                                   # the apiary checkout
  VERSION                                        # single-line semver pinned to the codebase
  .claude/
    settings.json                                # main-apiary's own per-repo hooks
    commands/*.md                                # main-apiary's own per-repo commands
    apiary/
      launch.py                                  # main-apiary's per-repo launcher
      main-apiary-pointer.json                   # points to itself
      self-pointer.json                          # records its own current path
      version.json                               # records its own pinned version
      flags/*-enabled                            # toggle flags
  CLAUDE.md                                      # apiary-managed zone + project rules
  .apiary/                                       # main-apiary-specific
    gui/apiary_gui[_profile]/                    # GUI state files
  .repos/                                        # the registry
    registry.json                                # uid → {name, real_path, version, ...}
    next_id                                      # monotonic counter
    <slug>/                                      # per-target state per registered repo
      scribe/                                    # notes, learnings, memory
      runner/                                    # runner artifacts
      compass/                                   # turn pairs, events, the rule table
      research/                                  # researcher findings
      sessions/                                  # per-repo session state
        history.json                             # per-repo session history (v1 schema)
        identity-<short>.json                    # per-session role/mission
      bootstrap_state.json                       # install hashes for drift detection (v2 schema)
  migrations/
    v0_<from>_to_v0_<to>.py                      # per-version migration scripts (kept indefinitely)

<bootstrapped-repo>/
  .claude/                                       # gitignored
    settings.json                                # apiary-installed hooks via per-repo launcher
    commands/*.md                                # copies of apiary slash commands
    apiary/
      launch.py                                  # thin shim → main-apiary
      main-apiary-pointer.json                   # path to main-apiary
      self-pointer.json                          # this repo's recorded path (drift detection)
      version.json                               # version pin
      flags/*-enabled                            # toggle flags
      session-tmp/                               # session-ephemeral hook flags
  CLAUDE.md                                      # apiary-managed zone + repo content
```

## Where every kind of state lives

| State | Location | Owner |
|---|---|---|
| Hook entries | `<repo>/.claude/settings.json` | per-repo |
| Slash command sources | `<main-apiary>/<tool>/commands/*.md`; copied to `<repo>/.claude/commands/` | per-repo (copies) |
| Per-repo launcher | `<repo>/.claude/apiary/launch.py` | per-repo (regenerable) |
| Bootstrap pointer to main-apiary | `<repo>/.claude/apiary/main-apiary-pointer.json` | per-repo |
| Self-location pointer | `<repo>/.claude/apiary/self-pointer.json` | per-repo |
| Version pin | `<repo>/.claude/apiary/version.json` | per-repo |
| Registry of all repos | `<main-apiary>/.repos/registry.json` | main-apiary |
| Migrations | `<main-apiary>/migrations/v<from>_to_v<to>.py` | main-apiary |
| Apiary-managed CLAUDE.md zone | `<repo>/CLAUDE.md` | per-repo |
| Toggle flags | `<repo>/.claude/apiary/flags/<flag>-enabled` | per-repo |
| Session-ephemeral hook flags | `<repo>/.claude/apiary/session-tmp/<sid>_*` | per-repo |
| Bootstrap manifest | `<main-apiary>/.repos/<slug>/bootstrap_state.json` | main-apiary |
| Per-target tool state | `<main-apiary>/.repos/<slug>/{scribe,runner,compass,...}/` | main-apiary |
| Per-target session history | `<main-apiary>/.repos/<slug>/sessions/history.json` | main-apiary |
| Per-target session identity | `<main-apiary>/.repos/<slug>/sessions/identity-<sid>.json` | main-apiary |
| GUI state | `<main-apiary>/.apiary/gui/` | main-apiary |
| Live Claude Code transcripts | `~/.claude/projects/<project-key>/*.jsonl` | Claude Code |

## Pin model — three small files per repo

### `<repo>/.claude/apiary/main-apiary-pointer.json`

```json
{
  "schema_version": 1,
  "main_apiary_path": "/abs/path/to/main-apiary",
  "main_apiary_uid": 1,
  "registered_at": "2026-05-05T22:14:33Z"
}
```

Absolute, machine-specific. Updated by main-apiary's cascade-fix when
main-apiary itself moves. Main-apiary's UID is always 1.

### `<repo>/.claude/apiary/self-pointer.json`

```json
{
  "schema_version": 1,
  "uid": 7,
  "name": "HexWorld",
  "real_path": "/abs/path/to/this/repo",
  "registered_at": "2026-05-05T22:14:33Z",
  "last_drift_check": "2026-05-05T22:14:33Z"
}
```

The source of truth for "where this repo thinks it lives." If `real_path`
differs from the actual cwd / git root, the repo has drifted (moved or copied).
**Critical:** must be in `.gitignore`. If committed, every clone has the wrong
path and the drift handler thinks the clone is the original-that-moved.

### `<repo>/.claude/apiary/version.json`

```json
{
  "schema_version": 1,
  "apiary_version": "0.1.0",
  "pinned_at": "2026-05-05T22:14:33Z"
}
```

Compared against `<main-apiary>/VERSION` on every session open. Mismatch
prompts the user to run `apiary update`.

## Drift detection (`core/drift.py`)

On the first tool call of a session in a bootstrapped repo, the PreToolUse
hook `core/hooks/per_repo_drift_check.py` — first in the dispatcher's chain,
guarded by a once-per-session flag file — runs:

1. Read self-pointer; if missing, the repo isn't bootstrapped. Skip silently.
2. Verify main-apiary is reachable: the path `main_apiary_path` exists and
   has its own valid self-pointer aligned with that path. If not, loud-warn
   and skip — sessions still run vanilla.
3. Compute drift = `self.real_path != cwd`.
4. No drift → refresh `last_drift_check` and return.
5. Drift → take `FileLock(<main-apiary>/.repos/registry.json)` and classify
   move-vs-copy, applying the registry update under that same lock:
   - **Copy**: registry entry's `real_path` exists with a self-pointer
     claiming the same uid → allocate a new uid via
     `core.utils.state.allocate_next_id`, overwrite our self-pointer with
     the new uid, then write a fresh registry entry for it.
   - **Move**: otherwise → update our self-pointer's `real_path` to cwd,
     then rewrite the existing entry's `real_path` + `last_used`. If the
     registry has no entry for our uid it is left alone and the report
     names `apiary install --target <repo>` as the repair.

The handler already holds the registry lock for the classification, so
there is no queue and no second consumer: the registry is correct the
moment the moved repo's first tool call returns.

Hooks must always exit 0 — a buggy drift handler can never block a tool call.

## Cascade-fix (`core/cascade.py`)

When main-apiary's own self-pointer drifts (main-apiary moved), main-apiary's
drift handler:

1. Updates main-apiary's own self-pointer + main-apiary-pointer + registry
   entry to the new location.
2. Walks the registry and rewrites every other bootstrapped repo's
   `<repo>/.claude/apiary/main-apiary-pointer.json` to the new path.
3. Skips entries whose `real_path` is gone or that lack a pin file —
   `apiary doctor unreachable` surfaces those for operator triage.

This is the only place main-apiary writes into other repos' files. Every
other code path is read-only with respect to bootstrapped repos.

## Versioning + migrations

- `<main-apiary>/VERSION` — single-line semver pinned to the codebase.
- `<repo>/.claude/apiary/version.json` — version this repo last bootstrapped
  or updated to.
- On every session open, the drift handler implicitly compares them.
  `apiary doctor versions` surfaces drift explicitly.
- `<main-apiary>/migrations/v<from>_to_v<to>.py` — version migration scripts.
  Each defines `FROM_VERSION`, `TO_VERSION`, and `upgrade(repo_path: Path)`.
  Idempotent + transactional contract per `migrations/README.md`.
- Kept indefinitely so a long-dormant repo coming back online can chain
  through every version.

## Doctor (`core/doctor.py`)

`apiary doctor` runs read-only consistency checks. Subsystems:

| Subcommand | Check | `--fix` action |
|---|---|---|
| `pointers` | Main-apiary's self-pointer matches its actual location | Cascade-fix all repos |
| `pins` | Each repo's `.claude/apiary/` pins agree with its registry entry (uid, name, main-apiary path), and uid 1 is main-apiary | Rewrite the disagreeing pins from the registry |
| `registry` | Every entry has uid + version; `real_path` exists | Report only |
| `versions` | Each repo's pinned version vs main-apiary's | Report which need `apiary update` |
| `stale` | Installed slash-command files differ from main-apiary source | Report only |
| `orphans` | `.repos/<slug>/` folders whose UID has no registry entry | Report only |
| `duplicates` | Two registry entries sharing a `real_path` | Report only |
| `unreachable` | Registry entries pointing at non-existent paths | Report only |
| `compass` | Rule-table health: captured sessions, classified events, heuristic turns, `rules.md` rows, the go/no-go verdict | Report only |
| (no arg) | All of the above in read-only mode | n/a |

Exit code is 0 when all checks pass and 1 when any reports an issue. Notes
(informational status) don't fail a run; only issues do. A `--fix` run exits 1
when an issue survives it — `pins` cannot decide, for instance, which repo
should keep uid 1.

## CLI surface

The `apiary` console_script (registered by `pyproject.toml`, source at
`core/cli.py`) is the unified entry point:

| Command | Description |
|---|---|
| `apiary install --target <repo>` | Bootstrap a target repo |
| `apiary uninstall --target <repo>` | Reverse install. `--remove-data` also deletes per-target state. |
| `apiary self-bootstrap` | Bootstrap main-apiary against itself |
| `apiary doctor [check]` | Consistency checks |
| `apiary cascade-fix` | Manually run cascade-fix |
| `apiary version` | Print main-apiary's pinned version |
| `apiary update` | Run the pending scripts in `migrations/` against every bootstrapped repo and re-pin it (`--target`, `--dry-run`) |

`scripts/install_repo_hooks.py` installs main-apiary's own
`.git/hooks/{pre-commit, post-merge}` (repo-local — unrelated to Claude
Code hooks).

## Subtle things

- **Main-apiary IS a bootstrapped repo.** It has its own `.claude/apiary/{...}`
  pin files. Its main-apiary-pointer.json points at itself. UID 1 by
  convention.
- **`name` is set at first bootstrap from the basename and doesn't change
  on move.** `myproject-7` stays `myproject-7` even after `mv myproject foo/`.
- **`version.json` is per-clone state, gitignored.** Different clones can
  pin different versions. On first session in a clone, the version-mismatch
  check fires.
- **Per-repo CLAUDE.md preserves user-owned content** around the apiary
  zone. `apiary install` only writes/updates the bounded zone.
- **`settings.json` has exactly one apiary-owned key: `hooks`.** Install
  regenerates the apiary-marked hook entries there and leaves the user's own
  alone. Every other key the profile carries is *merged* into the file — user
  entries survive, the profile's are added, and an entry the previous install
  contributed that the profile no longer ships is withdrawn (tracked in
  `bootstrap_state.json.profile_settings`). Keys the profile never mentions
  are never touched.
- **Apiary's hook entries carry an explicit mark.** Every generated command
  ends with the shell comment `# claude-apiary`, and that mark — not a guess
  from the path — is what install and uninstall use to tell their entries from
  the user's. Two pre-marker shapes are still recognized for cleanup: commands
  naming `apiary_launch.py` or `.claude/apiary/launch.py`, and absolute paths
  into a `claude-apiary` checkout that also name one of its hook directories.
- **Uninstall removes files first and the registry entry last**, and refuses
  outright when the target is main-apiary itself. A failure mid-way therefore
  leaves a repo that is still registered and still uninstallable, rather than
  one carrying pins and hooks that no registry entry accounts for.
- **Allocator is single source of truth.** `core.utils.state.allocate_next_id`
  is the sole UID allocator. Bootstrap and the drift handler's copy branch
  both use it under the registry FileLock. The one exception is deliberate:
  when the registry has lost a repo's entry but the repo still carries a
  self-pointer, `apiary install` re-adopts that uid (so `.repos/<name>-<uid>/`
  is not orphaned) and calls `state.reserve_uid` to push the counter past it.
- **`~/.claude/projects/<project-key>/`** is read-only from apiary's POV.
  Compass reads transcripts from there. Apiary never writes there — that's
  Claude Code's directory.
