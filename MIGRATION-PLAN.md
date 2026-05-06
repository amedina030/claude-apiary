# Apiary Per-Repo Migration Plan

> **Status:** design complete, implementation not started.
> **Drafted in session:** `88b37d36-5153-4d8a-aae9-a05de7556a16` (2026-05-05)
> **Successor session:** read this entire file before touching code. This document is the single source of truth for every architectural decision made for this migration. Where it conflicts with anything else (CLAUDE.md, docs/, prior scribe notes, agent reports), this document wins.
> **Scope:** move apiary from "global install in `~/.claude/`" to "fully per-repo install" with a thin three-pointer pin model. Rip out `--global` mode entirely once migrated.
> **When migration completes:** delete this file.

---

## 0. How to Use This Document

1. Read sections 1–4 first (purpose, glossary, decisions, architecture) — they orient you.
2. Sections 5–7 are the canonical migration spec — what each file becomes, what schema each new file uses, how each subsystem behaves after migration.
3. Section 8 lists what stays global and *why*. Don't try to move things in this list — they're either Claude Code's (we can't touch) or genuinely cross-repo by nature.
4. Section 9 is the risk register. Every risk has a mitigation; don't ship without addressing each one.
5. Section 10 is the phased rollout plan with an explicit ordering. Follow the phases — out-of-order changes will leave the system half-broken.
6. Section 11 is the verification checklist — what "done" means.
7. Section 12 documents where the explorer agent's risk-mapping report disagrees with this plan and why this plan wins. Read it; it'll save you re-litigating.
8. Section 13 lists subtle gotchas that are easy to miss.

> **Important:** this is a *plan*. Not all questions have been resolved. Section 14 lists open questions that need a real human decision before implementing those parts. Where the plan says "TBD" or "open question," do **not** silently invent an answer — surface it to the user.

---

## 1. Purpose & Motivation

Today, apiary installs into `~/.claude/` globally. One install, hooks fire in *every* Claude Code session on the machine, regardless of which repo (or whether any repo) the session is in. Slash commands, the launcher, the pointer file, GUI state, session history, the budgeter toggle flags, the apiary-managed CLAUDE.md zone — all global.

This has known drawbacks:

- **Blast radius.** A buggy hook breaks every session on the machine, even in repos that have nothing to do with apiary.
- **Implicit dependency.** Open `claude` in any directory and you're paying apiary's startup-hook cost and getting its CLAUDE.md zone in your prompt — even if you don't want that.
- **Single-version pointer.** `~/.claude/apiary.json` names exactly one apiary checkout. Multi-version dev / experimentation / parallel branches all require swapping pointers.
- **Discoverability.** Behavior triggered by global hooks looks "spooky" from inside an unrelated repo.

The goal of this migration is to make apiary **fully opt-in per repo**. A bootstrapped repo gets apiary's behavior; an un-bootstrapped repo gets a vanilla Claude Code session. The single global pointer becomes N per-repo pins. Updates are tracked via a versioned migration system. Drift between a repo's belief about its own location and its actual location is detected and reconciled lazily.

Per-target *data* (scribe notes, runner intakes, compass observations) stays centralized at `<main-apiary>/.repos/<slug>/` — that part of the architecture stays the same as today's `.repos/` registry. Only the install/routing layer moves per-repo.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **main-apiary** | The single apiary checkout that holds the source code, the registry of bootstrapped repos, all per-target state under `.repos/`, the migrations directory, and the GUI state. There is exactly one main-apiary on a machine. |
| **bootstrapped repo** | A repo that has been onboarded to apiary via `apiary install --target <repo>`. It has hooks in its own `.claude/settings.json`, its own slash commands, its own pointer files, and an entry in main-apiary's registry. Apiary behaves like a vanilla Claude Code session when invoked outside a bootstrapped repo. |
| **dual role** | main-apiary is itself a bootstrapped repo. It is registered in `<main-apiary>/.repos/registry.json` (currently as `claude-apiary-1`). Its UID is reserved at slot 1 by convention. |
| **pin model** | Each bootstrapped repo carries three small files identifying main-apiary, identifying itself, and pinning a version. Together they detect drift and let `apiary update` know what migrations to run. |
| **registry** | `<main-apiary>/.repos/registry.json` — already exists today. After migration it gains version + uid fields per entry but keeps the same schema shape. |
| **mailbox / forwarding inbox** | A directory at `<main-apiary>/.apiary/forwarding/<uid>.json` where bootstrapped repos drop drift-notification messages. Main-apiary processes these on its own startup (not at the moment they're written). One file per repo per pending message. |
| **drift** | Mismatch between a self-pointer's recorded path and the actual current path of the repo. Two flavors: *repo-drift* (a bootstrapped repo moved) and *main-apiary-drift* (main-apiary moved). |
| **slug** | The folder name under `.repos/` for a registered repo. Form: `<name>-<uid>`, e.g. `claude-apiary-1`, `HexMatCraft-2`. Already in use today. |
| **uid** | Monotonic integer allocated by `core/utils/state._allocate_next_id`. Stable across moves. Never collides (counter only goes up). |
| **legacy / global mode** | The current install model where everything lives in `~/.claude/`. Migration removes this entirely; there is no fallback to legacy behavior post-migration. |
| **opt-in semantics** | Sessions started in directories that aren't bootstrapped run as vanilla Claude Code, with no apiary hooks, no managed CLAUDE.md zone, no budgeter logging. Apiary is silent unless the cwd is a bootstrapped repo. |

---

## 3. Decision Index (every Q&A from session `88b37d36`)

These are the decisions agreed during the design conversation. They are final unless the next session explicitly overturns one with the user.

### 3.1 Pointer model

| # | Decision |
|---|---|
| D1 | Each bootstrapped repo has **three small JSON files** under `<repo>/.claude/apiary/`: `main-apiary-pointer.json`, `self-pointer.json`, `version.json`. (Schemas in §6.) |
| D2 | Self-pointer is the source of truth for "where this repo is." If self-pointer's recorded path differs from the actual current path, the repo has moved. |
| D3 | main-apiary itself has the same three files (it's a bootstrapped repo from its own POV). They live at `<main-apiary>/.claude/apiary/...` like any other. |
| D4 | When a bootstrapped repo's hook detects self-pointer drift, it does **not** edit main-apiary's registry directly. It writes a forwarding/mailbox message instead (see §3.2). |
| D5 | When main-apiary detects its own self-pointer drift, it walks its registry and rewrites every entry's `main-apiary-pointer.json` to the new path. This is the cascade-fix path. |
| D6 | UIDs come from `core/utils/state._allocate_next_id`. Monotonic ints. Never UUIDs. **Promoted to a public function** during this migration so bootstrap code, drift handler, and copy-detection all share one allocator. (See §6.1.) |

### 3.2 Drift notification (mailbox)

| # | Decision |
|---|---|
| D7 | Drift notifications use a **mailbox / outbox pattern**, not direct registry writes. Repos drop files at `<main-apiary>/.apiary/forwarding/<uid>.json`. main-apiary processes them on its own startup and on `apiary doctor mailbox`. |
| D8 | One forwarding file per repo per pending message. Schema in §6.4. Each file is atomic to write (`.tmp` + `os.replace`). main-apiary's processor deletes each file after a successful registry update. |
| D9 | **Before writing to the mailbox**, the repo verifies main-apiary actually exists at the path its `main-apiary-pointer.json` claims. If main-apiary is missing or moved (the path doesn't exist *or* the path exists but its self-pointer doesn't match), the repo issues a **loud warning to the session and skips** — does NOT create a ghost mailbox at a stale main-apiary location. (See §7.1 for the verification algorithm.) |

### 3.3 Versioning & migrations

| # | Decision |
|---|---|
| D10 | Versions are **semver** strings (e.g. `0.4.2`). Bumped manually on apiary releases. |
| D11 | At every session open in a bootstrapped repo, a hook compares the repo's pinned version against main-apiary's current version. On mismatch, the user is prompted to run `apiary update` (or it auto-runs — TBD §14). |
| D12 | Migrations live at `<main-apiary>/migrations/v<from>_to_v<to>.py`. They are **tracked in git, kept indefinitely**. NOT moved to an untracked `.scrap/` folder. The footprint is trivial (KB) and a long-dormant repo coming back online needs the chain. |
| D13 | `apiary update` chains migrations: jumping `0.3.0 → 0.5.0` runs `v0_3_to_v0_4.py` then `v0_4_to_v0_5.py` in order. Migrations must be idempotent and transactional (all-or-nothing per migration). |

### 3.4 Per-target state and session history

| # | Decision |
|---|---|
| D14 | Per-target state (scribe, runner, compass, research) **stays centralized** at `<main-apiary>/.repos/<slug>/{scribe,runner,compass,research,...}/`. Same as today. No change. |
| D15 | **Session history is per-repo** — but stored *inside main-apiary*, not inside the bootstrapped repo. Path: `<main-apiary>/.repos/<slug>/sessions/history.json`. Each repo gets its own bounded history file; no mega-aggregate JSON. |
| D16 | Same convention applies to other session-scoped apiary state: `<main-apiary>/.repos/<slug>/sessions/identity-<sid>.json`, `<main-apiary>/.repos/<slug>/sessions/transcripts/<sid>.jsonl`, etc. |
| D17 | Cross-repo session history goes away. If the user wants to look at "recent sessions in repo X," they ask Claude to read repo X's session-history file. Aggregating across repos is the GUI's job (it walks the registry). |

### 3.5 Hooks, commands, context rules, flags

| # | Decision |
|---|---|
| D18 | Hooks live in `<repo>/.claude/settings.json`, not in `~/.claude/settings.json`. They invoke the per-repo launcher at `<repo>/.claude/apiary/launch.py`. |
| D19 | Slash commands copied into `<repo>/.claude/commands/*.md` at bootstrap time. (Source remains each tool's `<main-apiary>/<tool>/commands/*.md`.) |
| D20 | The apiary-managed context-rules zone moves from `~/.claude/CLAUDE.md` to `<repo>/CLAUDE.md`. Still uses sentinel-bounded zone with hashed rule bodies. User-owned `~/.claude/CLAUDE.md` content is **not touched** by apiary going forward. |
| D21 | Toggle flags (`budgeter-log`, `budgeter-warn`, `budgeter-session-warn`, `auto-startup`) live at `<repo>/.claude/apiary/flags/<name>-enabled` (empty marker files). Per-repo. |

### 3.6 GUI

| # | Decision |
|---|---|
| D22 | GUI source code already lives in `<main-apiary>/gui/`. State files (tabs.json, sidebar_state.json, theme.json, launch.json, captures, permission_mcp_*) move from `~/.claude/apiary_gui/` to `<main-apiary>/.apiary/gui/`. |
| D23 | GUI operates *globally across all bootstrapped repos*: it reads the registry to enumerate repos, walks each one's `<main-apiary>/.repos/<slug>/sessions/` for per-repo session history, etc. One GUI process, N tabs/repos. |
| D24 | When main-apiary moves, the user's desktop shortcut to launch the GUI breaks until manually re-pinned. Acceptable. |

### 3.7 What stays global, what doesn't

| # | Decision |
|---|---|
| D25 | `--global` mode is **ripped out** entirely once migration completes. No fallback. No backward compat shim. No "hybrid" mode. After migration, the only way to onboard apiary to a repo is `apiary install --target <repo>`. |
| D26 | The `~/.claude/apiary_launch.py` global launcher is **removed**. Each bootstrapped repo has its own launcher under its own `.claude/apiary/`. |
| D27 | The `~/.claude/apiary.json` global pointer is **removed**. Each bootstrapped repo has its own per-repo `main-apiary-pointer.json`. |
| D28 | The `~/.claude/apiary_repos.json` registry is **removed**. The canonical registry is `<main-apiary>/.repos/registry.json` (already exists today, just gains fields). |
| D29 | Things that genuinely cannot move (Claude Code's own `~/.claude/projects/<key>/` transcript directory, Claude Code's session_id) stay where Claude Code puts them. Apiary continues to *read* from `~/.claude/projects/<key>/` for compass backfill. (See §8.) |

### 3.8 New tooling

| # | Decision |
|---|---|
| D30 | A new CLI: `apiary doctor`. Read-only by default, `--fix` applies safe fixes. Subsystems: `pointers`, `registry`, `mailbox`, `versions`, `orphans`, `duplicates`, `unreachable`. (See §7.4.) |
| D31 | A new CLI flow: `apiary self-bootstrap` for first-machine setup. Initializes main-apiary's own pointer files, creates an empty registry containing only itself, sets the version. |
| D32 | When main-apiary or a registered repo is unreachable at session open, behavior is **loud warn + skip**. Not silent skip. Not hard fail. Print a clear message in the session, then run as a vanilla Claude Code session. |

### 3.9 Acceptance / accepted tradeoffs

| # | Decision |
|---|---|
| D33 | Onboarding tax (per-repo bootstrap) is accepted — opt-in is the goal. |
| D34 | Per-repo budgeter coverage (only bootstrapped repos get cost tracking) is accepted. |
| D35 | Cross-repo session history aggregation as a single file is dropped. |
| D36 | `apiary doctor` is the operator escape hatch when consistency goes weird. Hooks should not auto-self-heal complex states. |

### 3.10 Copy detection

| # | Decision |
|---|---|
| D37 | When a repo's hook detects self-pointer drift, it disambiguates "I moved" vs "I'm a copy of another repo" before queueing a mailbox message: |
| | (a) Read main-apiary's registry, find the entry for our recorded UID. |
| | (b) Look at `registry[uid].real_path`. If that path exists on disk AND the repo there has a self-pointer claiming the same UID → **we are a copy**. Allocate a new UID, overwrite our self-pointer with `{cwd, new_uid, version}`, queue a `register` (not `update`) mailbox message. |
| | (c) Otherwise (path doesn't exist or its self-pointer claims a different UID) → **we are the original, just moved**. Queue an `update` mailbox message. |
| D38 | Brand-new copies trigger this branch on their *first* session because their copied self-pointer has the original's path. That's the intended trigger point. |

---

## 4. Final Architecture

### 4.1 File-tree picture

```
~/.claude/                                       # ENDS UP NEARLY EMPTY
  CLAUDE.md                                      # USER-OWNED ONLY (apiary doesn't write here anymore)
  projects/                                      # CLAUDE CODE OWNS (untouched)
    <project-key>/
      *.jsonl                                    # transcripts apiary READS for compass
  settings.json                                  # CLAUDE CODE'S — apiary stops writing hooks here
  commands/                                      # apiary stops installing here
  # everything else apiary used to put here is GONE post-migration

<main-apiary>/                                   # the apiary checkout itself
  .claude/
    settings.json                                # main-apiary's own per-repo hooks
    commands/*.md                                # main-apiary's own per-repo commands
    apiary/
      launch.py                                  # main-apiary's per-repo launcher (it's a bootstrapped repo)
      main-apiary-pointer.json                   # points to itself
      self-pointer.json                          # records its own current path
      version.json                               # records its own pinned version
      flags/*-enabled                            # main-apiary's own toggle flags
      session-tmp/                               # session-ephemeral flags (install_checked, etc.)
  CLAUDE.md                                      # main-apiary's project rules + apiary-managed zone
  .apiary/                                       # MAIN-APIARY-SPECIFIC (not in other bootstrapped repos)
    forwarding/
      <uid>.json                                 # mailbox messages from bootstrapped repos
    gui/
      tabs.json
      sidebar_state.json
      theme.json
      launch.json
      composer_state.json
      captures/<ts>-<label>.bin
      permission_mcp_config.json
      permission_mcp.log
  .repos/                                        # ALREADY EXISTS — extended by migration
    registry.json                                # gains uid + version fields per entry
    next_id                                      # monotonic counter (already exists)
    <slug>/                                      # per-target state per registered repo
      scribe/
      runner/
      compass/
      research/
      sessions/                                  # NEW: per-repo session state
        history.json                             # NEW: per-repo session history
        identity-<sid>.json                      # NEW: per-repo per-session identity
        transcripts/<sid>.jsonl                  # NEW: per-repo transcript archive
      bootstrap_state.json                       # ALREADY EXISTS — extended with profile hash + version
  migrations/
    v0_3_to_v0_4.py                              # NEW: version migration scripts (tracked, kept indefinitely)
    v0_4_to_v0_5.py
    ...
  budgeter/, scribe/, runner/, compass/, ...     # source code unchanged

<bootstrapped-repo>/                             # any other repo onboarded via `apiary install`
  .claude/
    settings.json                                # apiary-installed hooks, all reference $CLAUDE_PROJECT_DIR/.claude/apiary/launch.py
    commands/*.md                                # copies of apiary slash commands
    apiary/
      launch.py                                  # thin shim; reads main-apiary-pointer.json, dispatches to main-apiary's code
      main-apiary-pointer.json                   # path to main-apiary
      self-pointer.json                          # this repo's recorded path (drift detection)
      version.json                               # version pin
      flags/*-enabled                            # toggle flags
      session-tmp/                               # session-ephemeral flags
  CLAUDE.md                                      # may be empty/user-owned + apiary-managed zone
```

### 4.2 Where every kind of state lives, after migration

| State | Today | Post-migration | Owner |
|---|---|---|---|
| Hook entries | `~/.claude/settings.json` | `<repo>/.claude/settings.json` | per-repo |
| Slash command sources | each tool's `*/commands/*.md` | unchanged source; copied to `<repo>/.claude/commands/*.md` | per-repo (copies) |
| Launcher | `~/.claude/apiary_launch.py` | `<repo>/.claude/apiary/launch.py` | per-repo |
| Bootstrap pointer to main-apiary | `~/.claude/apiary.json` | `<repo>/.claude/apiary/main-apiary-pointer.json` | per-repo |
| Self-location pointer | (didn't exist) | `<repo>/.claude/apiary/self-pointer.json` | per-repo |
| Version pin | (didn't exist) | `<repo>/.claude/apiary/version.json` | per-repo |
| Registry of all repos | `~/.claude/apiary_repos.json` + `<main-apiary>/.repos/registry.json` | `<main-apiary>/.repos/registry.json` only | main-apiary |
| Drift mailbox | (didn't exist) | `<main-apiary>/.apiary/forwarding/<uid>.json` | main-apiary |
| Migrations | (didn't exist) | `<main-apiary>/migrations/v<from>_to_v<to>.py` | main-apiary |
| Apiary-managed CLAUDE.md zone | `~/.claude/CLAUDE.md` | `<repo>/CLAUDE.md` | per-repo |
| Toggle flags (budgeter-*, auto-startup) | `~/.claude/<flag>-enabled` | `<repo>/.claude/apiary/flags/<flag>-enabled` | per-repo |
| Session-ephemeral hook flags (install_checked, etc.) | `~/.claude/tmp/<sid>_*` | `<repo>/.claude/apiary/session-tmp/<sid>_*` | per-repo |
| Install manifest | `~/.claude/.install-manifest.json` | superseded by `<main-apiary>/.repos/<slug>/bootstrap_state.json` (already exists, gains hashes) | main-apiary |
| Per-target scribe/runner/compass/research state | `<main-apiary>/.repos/<slug>/...` | unchanged | main-apiary |
| Per-target session history | `~/.claude/.session-history.json` (cross-repo aggregate) | `<main-apiary>/.repos/<slug>/sessions/history.json` per-repo | main-apiary |
| Per-target session identity | `~/.claude/.session-identity-<sid>.json` (one user-global file per session) | `<main-apiary>/.repos/<slug>/sessions/identity-<sid>.json` per-repo | main-apiary |
| Transcript archives | `~/.claude/transcripts/<sid>.jsonl` | `<main-apiary>/.repos/<slug>/sessions/transcripts/<sid>.jsonl` | main-apiary |
| GUI state | `~/.claude/apiary_gui/...` | `<main-apiary>/.apiary/gui/...` | main-apiary |
| Claude Code session transcripts (live) | `~/.claude/projects/<key>/*.jsonl` | unchanged — Claude Code owns this path | Claude Code |
| User-owned global rules | `~/.claude/CLAUDE.md` (mixed with apiary zone today) | `~/.claude/CLAUDE.md` (untouched by apiary going forward) | user |

---

## 5. Per-Repo File Layout (the new files)

Every bootstrapped repo, including main-apiary, has this structure:

```
<repo>/
  .claude/
    settings.json                                # generated by apiary install
    commands/<slash-command>.md                  # copied from main-apiary's */commands/
    apiary/
      launch.py                                  # generated; thin shim
      main-apiary-pointer.json                   # generated
      self-pointer.json                          # generated; gitignored
      version.json                               # generated
      flags/                                     # toggle flags
      session-tmp/                               # session-ephemeral hook flags
  CLAUDE.md                                      # apiary-managed zone added here
  .gitignore                                     # updated to gitignore .claude/ entries
```

### 5.1 Gitignore policy for bootstrapped repos

The whole `.claude/` directory is gitignored on bootstrapped repos. Rationale:

- `settings.json` references `$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py` — portable across machines, but regenerable by `apiary install`. No reason to track.
- `commands/*.md` are copies of main-apiary's source; tracking them creates a stale-copy problem when main-apiary updates.
- `apiary/main-apiary-pointer.json` is machine-specific (absolute path).
- `apiary/self-pointer.json` is machine-specific (absolute path) AND must not be committed (clones would propagate the wrong path). Critical: see §9.4.
- `apiary/version.json` is local-clone state (the version this clone is pinned to). Different clones can be on different versions.
- `apiary/flags/` and `apiary/session-tmp/` are runtime ephemera.

`<repo>/CLAUDE.md` **is tracked** (it's the project's CLAUDE.md and may contain user-owned content alongside the apiary-managed zone).

If a bootstrapped repo doesn't already have `.claude/` in `.gitignore`, `apiary install` adds it.

---

## 6. Per-Repo File Schemas

### 6.1 UID allocation

UIDs are allocated by `core/utils/state._allocate_next_id(main_apiary_path)`. This function exists today (`core/utils/state.py:122`). It atomically reads `<main-apiary>/.repos/next_id`, increments, writes back. Caller must hold the registry FileLock during allocation (already part of the existing contract).

**Migration task:** rename `_allocate_next_id` → `allocate_next_id` (drop the underscore) and document its public contract. Update all callers. Do not introduce a parallel ID-generator. Specifically: the bootstrap flow, the copy-detection branch in the drift handler, and the mailbox processor all use this single function.

UIDs are monotonic ints starting from 1. main-apiary itself has UID 1 by convention (already true in today's registry). New UIDs only ever go up, so they never collide with existing entries.

### 6.2 `<repo>/.claude/apiary/main-apiary-pointer.json`

```json
{
  "schema_version": 1,
  "main_apiary_path": "/abs/path/to/main-apiary",
  "main_apiary_uid": 1,
  "registered_at": "2026-05-05T22:14:33Z"
}
```

- `main_apiary_path` is absolute, machine-specific, gitignored. Updated by main-apiary's cascade-fix when main-apiary moves.
- `main_apiary_uid` is always 1 (main-apiary's own UID).
- `registered_at` is set at first bootstrap; never updated.

### 6.3 `<repo>/.claude/apiary/self-pointer.json`

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

- `uid` is the UID main-apiary's registry knows this repo by.
- `name` is the slug-name part (used to form `<name>-<uid>` for the registry folder).
- `real_path` is what self-pointer asserts. If `Path.cwd()` (or the repo's git root) doesn't match `real_path` → drift.
- `last_drift_check` is updated on every session-startup hook fire (so we know when last verified).

**Critical:** must be in `.gitignore`. If committed, every clone has the wrong path and the drift handler thinks the clone is the original-that-moved.

### 6.4 `<repo>/.claude/apiary/version.json`

```json
{
  "schema_version": 1,
  "apiary_version": "0.4.2",
  "pinned_at": "2026-05-05T22:14:33Z"
}
```

- `apiary_version` is semver. Bumped manually on apiary releases.
- `pinned_at` is set when this repo last successfully ran `apiary install` or `apiary update`.

### 6.5 `<main-apiary>/.repos/registry.json` (extended)

Today's schema (from `<main-apiary>/.repos/registry.json`):

```json
{
  "1": {
    "name": "claude-apiary",
    "real_path": "D:\\Professional\\claude-apiary",
    "registered_at": "2026-05-05T01:49:12Z",
    "last_used": "2026-05-06T00:38:12Z",
    "last_verified": "2026-05-05T02:12:48Z",
    "verified_ok": true
  }
}
```

After migration, each entry gains:

```json
{
  "1": {
    "name": "claude-apiary",
    "real_path": "D:\\Professional\\claude-apiary",
    "uid": 1,
    "version": "0.4.2",
    "registered_at": "2026-05-05T01:49:12Z",
    "last_used": "2026-05-06T00:38:12Z",
    "last_verified": "2026-05-05T02:12:48Z",
    "verified_ok": true
  }
}
```

The keys are the UIDs as strings (already true today). `uid` is duplicated as a field for clarity. `version` is the version this repo last bootstrapped or updated to.

### 6.6 `<main-apiary>/.apiary/forwarding/<uid>.json` (mailbox)

```json
{
  "schema_version": 1,
  "from_uid": 7,
  "kind": "update_path" | "register_copy",
  "old_path": "/abs/old",
  "new_path": "/abs/new",
  "name": "HexWorld",
  "version": "0.4.2",
  "ts": "2026-05-05T22:14:33Z"
}
```

- `kind: update_path` — repo moved; main-apiary should rewrite `registry[uid].real_path` to `new_path`.
- `kind: register_copy` — a copy was detected; main-apiary should allocate a new UID and register a fresh entry. The caller provides `name` and `new_path`. Main-apiary writes the new UID back into the copy's self-pointer (lazily, via the caller re-reading on next session — see §7.2).

main-apiary deletes each forwarding file after successful processing.

### 6.7 `<main-apiary>/.repos/<slug>/sessions/history.json`

```json
{
  "schema_version": 1,
  "sessions": [
    {
      "session_id": "abc123...",
      "started_at": "...",
      "ended_at": "...",
      "transcript_path": "/abs/path/to/transcript.jsonl",
      "role": "user",
      "mission": "general",
      "registered": true
    }
  ]
}
```

Same fields as today's `~/.claude/.session-history.json`, just one file per registered repo. Bounded growth (one repo's worth of sessions, not all sessions across all repos).

### 6.8 `<main-apiary>/.repos/<slug>/bootstrap_state.json` (extended)

Already exists today (per agent finding). After migration it must include enough info to detect drift:

```json
{
  "schema_version": 2,
  "profile": "base",
  "profile_resolved_hash": "<sha256 of resolved profile JSON>",
  "apiary_version": "0.4.2",
  "settings_json_hash": "<sha256 of generated settings.json>",
  "commands_dir_hashes": {"note.md": "<sha>", ...},
  "claude_md_zone_hash": "<sha of apiary-managed zone in <repo>/CLAUDE.md>",
  "bootstrapped_at": "...",
  "last_updated_at": "..."
}
```

`apiary doctor registry` and the per-session drift check use these hashes to detect if a user has hand-edited files apiary owns.

---

## 7. Component Designs

### 7.1 Main-apiary verification (precondition for any mailbox write)

Before a bootstrapped repo writes to the mailbox, it must verify main-apiary actually exists at the path its `main-apiary-pointer.json` claims. Algorithm:

```
1. read self-pointer; compute drift = self_pointer.real_path != current_repo_root
2. read main-apiary-pointer; M = main_apiary_path
3. check Path(M).is_dir()
   - false → loud warn ("apiary main checkout not found at M; running as vanilla session"); return SKIP
4. check Path(M / ".claude/apiary/self-pointer.json").is_file()
   - false → loud warn ("M is not a valid main-apiary checkout"); return SKIP
5. read main-apiary's self-pointer at M
   - main_self.real_path == M? → main-apiary is consistent; proceed
   - main_self.real_path != M? → main-apiary moved but cascade-fix didn't run yet; loud warn ("main-apiary self-pointer out of sync; run `apiary doctor pointers` from <main_self.real_path>"); return SKIP
6. proceed with drift handling (§7.2)
```

The "loud warn" is a one-line message printed to the session via the hook's stdout (visible to the user in the conversation). It must be specific: name the path that failed and the next step.

### 7.2 Drift handling (per-repo, at session open)

This logic runs in the per-repo PreToolUse hook on first tool call of each session. After §7.1 returns "proceed":

```
1. if not drift: write self_pointer.last_drift_check = now; return
2. drift detected. resolve UID:
   a. read self-pointer; uid = self_pointer.uid
   b. read main-apiary's registry; entry = registry[uid]
3. classify:
   a. if entry exists AND Path(entry.real_path).is_dir() AND that dir has a self-pointer
      with the same uid → COPY scenario:
        - allocate new uid via allocate_next_id(main_apiary)
        - overwrite OUR self-pointer with {uid: new_uid, real_path: cwd, name: ..., version: ...}
        - write a `register_copy` mailbox message with from_uid=new_uid, new_path=cwd, name, version
        - log "detected copy of repo uid=<old>; registered as new uid=<new>"
   b. otherwise → MOVE scenario:
        - update OUR self-pointer's real_path to cwd, last_drift_check = now
        - write an `update_path` mailbox message with from_uid=uid, old_path=entry.real_path, new_path=cwd
        - log "detected move; queued registry update for uid=<uid>"
4. proceed with normal session startup
```

Important: this runs in a hook. It must not block the session for more than ~100ms in the normal (no-drift) case. The drift case can be slower (file I/O for the mailbox write) but should still be sub-second.

### 7.3 Cascade-fix (when main-apiary itself moves)

When a session opens *in main-apiary itself* and main-apiary's self-pointer has drifted:

```
1. main-apiary's PreToolUse hook detects self-pointer drift (via the same algorithm in §7.2 — main-apiary IS a bootstrapped repo)
2. main-apiary processes its OWN drift first: update its own self-pointer to cwd
3. then invoke the cascade fix:
   a. read registry
   b. for each entry where uid != 1 (skip self):
        - resolve <repo> = Path(entry.real_path)
        - if Path(<repo>/.claude/apiary/main-apiary-pointer.json).is_file():
            - rewrite that pointer to {main_apiary_path: new_main_apiary_path, ...}
        - else: skip (repo is gone or not bootstrapped properly)
   c. log "cascade-fix updated N pointers"
4. proceed
```

If any registered repo's `real_path` doesn't exist on disk during the cascade, it's skipped silently — `apiary doctor unreachable` will surface it later.

### 7.4 Mailbox processing (main-apiary side)

main-apiary processes its mailbox at:
- session open in main-apiary (via the same hook that does drift detection)
- `apiary doctor mailbox` (manual)
- the GUI on its own startup

Algorithm:

```
1. acquire FileLock on registry
2. for each forwarding file in <main-apiary>/.apiary/forwarding/*.json:
     a. read message
     b. switch (message.kind):
          update_path: registry[message.from_uid].real_path = message.new_path
                       registry[message.from_uid].last_used = message.ts
          register_copy: new_uid = allocate_next_id()
                         registry[new_uid] = {name, real_path: new_path, uid: new_uid, version, ...}
                         (the copy will pick up its assigned uid by reading registry on its next session)
     c. atomic delete the forwarding file (.tmp + os.replace + os.unlink)
3. release lock
```

Concurrency: since there is exactly one main-apiary process touching the registry at a time (FileLock-enforced), the mailbox is single-consumer. Bootstrapped repos are multiple producers, but each writes its own uid-named file (no collision).

### 7.5 Versioning, migrations, `apiary update`

Every session in a bootstrapped repo runs (in the startup hook):

```
1. read <repo>/.claude/apiary/version.json; pinned = ver
2. read <main-apiary>/version.json (or wherever main-apiary records its current version);
   current = ver
3. if pinned != current:
     emit a one-line warning to the session:
       "apiary version drift: this repo is pinned to <pinned>, main-apiary is at <current>.
        Run `apiary update` to migrate."
4. proceed
```

`apiary update` (CLI):

```
1. resolve current repo (the one we're in)
2. read pinned and current versions
3. find migration chain: every migration file v<a>_to_v<b>.py where a >= pinned and b <= current,
   ordered by from-version
4. for each migration in order:
     a. import the module
     b. run module.upgrade(<repo>) under a try/except/rollback wrapper
     c. on success, update version.json's apiary_version to b
     d. on failure: revert to pre-migration state, abort the chain, surface the error
5. update <main-apiary>/.repos/<slug>/bootstrap_state.json with new version + hashes
```

Migration files have this shape:

```python
# <main-apiary>/migrations/v0_3_to_v0_4.py
"""Migrate a bootstrapped repo from apiary 0.3.x to 0.4.x.

What changes: <one-line description>
Idempotent: yes (safe to run twice).
"""
from pathlib import Path

FROM_VERSION = "0.3"
TO_VERSION = "0.4"

def upgrade(repo_path: Path) -> None:
    """Apply the migration. Must be idempotent and atomic.
    Raise on failure — caller will roll back."""
    ...
```

Migrations are kept in git indefinitely (D12). Never moved to `.scrap/` or untracked locations. An old laptop coming online with a v0.1 repo must be able to chain v0.1→...→current using migrations that ship with the current main-apiary checkout.

Auto-run vs prompted: see open question §14.1.

### 7.6 `apiary doctor`

A new CLI at `<main-apiary>/core/doctor.py` (or similar) wired into `setup.py`/`apiary` entry points.

Subsystems and what each checks (read-only by default; `--fix` applies safe fixes; `--dry-run` shows what would change):

| Subcommand | Check | --fix action |
|---|---|---|
| `apiary doctor pointers` | main-apiary's self-pointer matches its actual cwd; if drifted, run cascade-fix | rewrite main-apiary's self-pointer + cascade-fix all repos |
| `apiary doctor registry` | walk every registered repo: path exists; self-pointer matches; uid/version fields present; bootstrap_state.json hashes match generated content | report only (no auto-fix; needs human triage) |
| `apiary doctor mailbox` | drain pending forwarding messages | process and apply |
| `apiary doctor versions` | each repo's pinned version vs main-apiary's version | report which repos need `apiary update` |
| `apiary doctor orphans` | folders under `.repos/<slug>/` whose UID has no registry entry | with `--fix`, prompt to delete each |
| `apiary doctor duplicates` | two registry entries with the same `real_path` (shouldn't happen) | report only |
| `apiary doctor unreachable` | registry entries pointing at paths that don't exist | with `--fix`, prompt to deregister each |
| `apiary doctor` (no arg) | run all of the above in read-only mode; print a summary | n/a |

`apiary doctor` is the operator escape hatch. Hooks should never auto-self-heal complex states — they detect and either fix the simple cases (mailbox-queued drift) or warn and skip. Doctor handles everything else.

### 7.7 Per-repo launcher (`<repo>/.claude/apiary/launch.py`)

Generated by `apiary install --target <repo>` at bootstrap. Shape:

```python
#!/usr/bin/env python3
"""Apiary per-repo launcher. Generated by `apiary install`. Do not edit.
Reads main-apiary-pointer.json and dispatches to a script in main-apiary.
"""
import json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
POINTER = HERE / "main-apiary-pointer.json"

def _resolve_main_apiary():
    if not POINTER.is_file():
        return None
    try:
        return Path(json.loads(POINTER.read_text(encoding="utf-8"))["main_apiary_path"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None

def main():
    main_apiary = _resolve_main_apiary()
    if main_apiary is None or not main_apiary.is_dir():
        print(f"[apiary] main-apiary not found via {POINTER} — running as vanilla session", file=sys.stderr)
        sys.exit(0)  # do not block the session

    if len(sys.argv) < 2:
        print("[apiary] usage: launch.py <script-relative-to-main-apiary> [args...]", file=sys.stderr)
        sys.exit(2)

    script = main_apiary / sys.argv[1]
    if not script.is_file():
        print(f"[apiary] script not found: {script}", file=sys.stderr)
        sys.exit(0)

    # Set env vars so downstream code can find main-apiary and per-target state
    env = os.environ.copy()
    env["APIARY_MAIN_REPO"] = str(main_apiary)
    # (resolution of APIARY_TARGET_STATE_DIR happens inside the dispatched script
    #  via core.utils.state.resolve_target_state_dir, same as today)

    sys.exit(subprocess.run([sys.executable, str(script), *sys.argv[2:]], env=env).returncode)

if __name__ == "__main__":
    main()
```

Same contract as today's `~/.claude/apiary_launch.py`:

- Inherits caller cwd unchanged (does not chdir into main-apiary).
- Sets `APIARY_MAIN_REPO` env var (replaces the role of `~/.claude/apiary.json` in resolution).
- Subprocess inherits all other env vars (CLAUDE_PROJECT_DIR, etc.).

Hook entries in `<repo>/.claude/settings.json` look like:

```
"command": "python \"$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py\" core/hooks/startup_hook.py"
```

### 7.8 What `apiary install --target <repo>` does

1. Resolve `<repo>` to its git root.
2. Acquire registry FileLock.
3. Look up entry by `real_path` in `<main-apiary>/.repos/registry.json`.
   - exists → reuse uid, name, slug
   - missing → allocate new uid via `allocate_next_id()`; pick name from repo dir basename; create registry entry
4. Create `<main-apiary>/.repos/<slug>/` if missing (sessions/, scribe/, etc.).
5. Generate the per-repo files in `<repo>/.claude/apiary/`:
   - `launch.py` (template above, written verbatim)
   - `main-apiary-pointer.json`
   - `self-pointer.json`
   - `version.json`
   - `flags/` (empty)
   - `session-tmp/` (empty)
6. Write `<repo>/.claude/settings.json` from the resolved profile (existing logic in `core/apiary_bootstrap.py` already does this; only the install path changes).
7. Copy `<main-apiary>/<tool>/commands/*.md` → `<repo>/.claude/commands/`.
8. Write apiary-managed zone into `<repo>/CLAUDE.md` (same sentinel-bounded zone as today's `~/.claude/CLAUDE.md` flow, just the target file changes).
9. Update `<repo>/.gitignore` to ignore `.claude/`.
10. Compute hashes; write/update `<main-apiary>/.repos/<slug>/bootstrap_state.json`.
11. Release lock.

`apiary install` is **idempotent**. Re-running on a bootstrapped repo refreshes all generated files and regenerates the bootstrap_state.json hashes.

### 7.9 What `apiary self-bootstrap` does (fresh-machine setup)

For the very first install on a new machine:

1. Verify cwd is the apiary checkout (looks for sentinel files: `core/apiary_bootstrap.py`, `migrations/`, etc.).
2. Initialize `<main-apiary>/.repos/registry.json` with just main-apiary itself at uid=1.
3. Initialize `<main-apiary>/.repos/next_id` to `2`.
4. Run the equivalent of `apiary install --target <main-apiary>` on itself.
5. Done.

Other repos are then bootstrapped via `apiary install --target <other-repo>` from inside main-apiary (or from anywhere — the CLI knows main-apiary because cwd is its repo or the user passes `--main-apiary <path>`).

### 7.10 What `apiary update` does

See §7.5.

### 7.11 What `apiary uninstall --target <repo>` does

1. Acquire registry lock.
2. Remove `<repo>/.claude/apiary/`.
3. Remove `<repo>/.claude/commands/<our-files>.md` (only the ones we copied; identified via `bootstrap_state.json.commands_dir_hashes`).
4. Remove apiary-managed zone from `<repo>/.claude/settings.json` and from `<repo>/CLAUDE.md` (use existing zone-detection logic in `core/hooks_lib.py`).
5. Remove the `<main-apiary>/.repos/<slug>/` folder (all per-target state — scribe, runner, sessions, etc.). **Confirm with user before destructive delete.**
6. Remove the registry entry.
7. Release lock.

A `--keep-data` variant skips step 5 (leaves per-target state intact for archival).

### 7.12 Removal of global mode

After all 4 currently-bootstrapped repos (`claude-apiary-1`, `HexMatCraft-2`, `HexWorld-3`, `HexWorld-5.7-4`) are migrated, the cleanup script removes:

- `~/.claude/apiary_launch.py`
- `~/.claude/apiary.json`
- `~/.claude/apiary_repos.json`
- `~/.claude/apiary_bootstrap.py`
- `~/.claude/.install-manifest.json`
- `~/.claude/apiary_gui/` and `~/.claude/apiary_gui_dev/` (after copying state to `<main-apiary>/.apiary/gui/`)
- `~/.claude/budgeter-log-enabled`, `~/.claude/budgeter-warn-enabled`, `~/.claude/budgeter-session-warn-enabled`, `~/.claude/auto-startup-enabled` (after copying state to per-repo flags)
- `~/.claude/commands/<apiary-files>.md` (16 files — identified by name list)
- `~/.claude/.session-history.json` (after archiving any remaining global entries to per-repo histories — see §10 phase 4)
- `~/.claude/.last-transcript.jsonl`, `~/.claude/transcripts/` (after archiving)
- `~/.claude/.session-identity-*` files (after archiving relevant ones to per-repo)
- The apiary-managed zone in `~/.claude/CLAUDE.md` (just the zone — preserve any user-owned content around it)
- All apiary-tagged hook entries in `~/.claude/settings.json` (use existing `is_apiary_entry` logic in `core/hooks_lib.py`)

`setup.py --global` is removed entirely. Setup.py either exits with an error directing the user to `apiary install --target <repo>`, or is renamed/repurposed. (TBD §14.5.)

---

## 8. What Stays Global — and Why

Some things genuinely cannot or should not move per-repo. The migration must respect these.

### 8.1 Things Claude Code owns (cannot touch)

| Path | Owner | Apiary's relationship |
|---|---|---|
| `~/.claude/projects/<project-key>/*.jsonl` | Claude Code | Apiary **reads** these (compass backfill at `compass/backfill.py:42`, transcript extraction at `core/hooks/save_transcript.py`). Apiary does NOT write to this directory. The path is owned by Claude Code; if Claude Code changes where it writes transcripts, apiary's read paths must change too. |
| `~/.claude/settings.json` (the file itself) | Claude Code | Apiary stops writing hook entries here. The file remains; Claude Code uses it for global non-apiary settings. |
| `~/.claude/CLAUDE.md` (the file itself) | Claude Code (user) | Apiary stops writing the managed zone here. The file remains; user-owned global rules stay unchanged. |
| Claude Code's session_id | Claude Code | Apiary observes it (used to key per-session files) but doesn't generate or own it. |

### 8.2 Things that are user-global by nature

(None. Every prior candidate moved into main-apiary or per-repo.)

### 8.3 Things in `~/.claude/` that are explicitly removed by migration

See §7.12 for the full list.

---

## 9. Risks and Mitigations

The explorer agent (subagent run in session `88b37d36`) produced a 32-file risk map. Where it disagreed with this plan, see §12. The legitimate risks it surfaced — plus several that came up during the design — are below. Each risk has an owner section in this document where the mitigation is detailed.

### 9.1 Tests pinned to global paths

Many tests mock `Path.home()` and assert paths under `~/.claude/`. Identified files (non-exhaustive):

- `core/test_setup_check.py` (lines 98–134): mocks Path.home, installs into tmpdir's `.claude/`
- `core/test_apiary_bootstrap.py` (multiple — lines 116, 256, 315, 421–478): bootstrap tests using Path.home mocking
- `core/test_apiary_launch.py` (lines 44–59): launcher tests
- `budgeter/test_hooks.py` (line 753): flag toggle tests
- `core/hooks/test_save_transcript.py` (line 44): session-history tests
- `core/hooks/test_runner_subprocess_guards.py` (line 6): flag-file path tests

**Severity:** HIGH (tests will fail without updates).
**Mitigation:** every test that mocks `Path.home()` for apiary state must be rewritten to mock the per-repo path or main-apiary's `.repos/<slug>/` instead. Some tests can simply switch their assertion target. Others (bootstrap, install) need to mock both a fake "main-apiary" tmpdir and a fake "target repo" tmpdir.

### 9.2 Backward compat for old in-repo state

Some pre-migration repos have legacy state at `<repo>/.apiary/scribe/` (per `C-2026-46`, the recent state migration). The new design uses `<main-apiary>/.repos/<slug>/scribe/` exclusively. Apiary's resolver (`core/utils/state.resolve_target_state_dir`) already handles this with `APIARY_STATE_LAYOUT=legacy` as an escape hatch.

**Severity:** MEDIUM.
**Mitigation:** the migration plan does not introduce *new* legacy locations. Existing repos already migrated to `.repos/<slug>/` are unaffected. Repos that haven't migrated yet should be migrated as part of phase 0 of this plan (run the existing `scripts/migrate_to_repos.py`). After this plan's migration, drop the `APIARY_STATE_LAYOUT=legacy` escape hatch from the launcher and `core/utils/state.py`.

### 9.3 Project-key derivation gotchas

Compass and other tools use a project-key (hash of cwd) to find Claude Code's transcripts at `~/.claude/projects/<key>/`. Two key derivations exist (per learnings `L-2026-41`, `L-2026-97`):

1. cwd-derived (path-hash, machine-specific)
2. stable file-derived (`.claude-project-key` in repo root, portable)

**Severity:** MEDIUM.
**Mitigation:** no change to this logic in this migration. Compass continues to read from `~/.claude/projects/<key>/` (Claude Code owns that path). The fallback order (stable → legacy) at `compass/backfill.py:60` and `scribe/notes.py` continues to work.

### 9.4 Self-pointer must not be committed

If `<repo>/.claude/apiary/self-pointer.json` is committed to git, every clone of that repo has the wrong path. First session in a clone "detects drift" and tries to overwrite main-apiary's registry to point at the clone — fighting with the original.

**Severity:** BLOCKER if violated.
**Mitigation:** `apiary install` writes `.claude/` (whole dir) into `<repo>/.gitignore` if not already there. Add a startup-hook safety check: if the self-pointer file is *tracked* by git (run `git ls-files --error-unmatch .claude/apiary/self-pointer.json`), the hook refuses to operate and prints a fix instruction.

### 9.5 Copy-vs-move ambiguity from `cp -r`

If a user `cp -r`s a bootstrapped repo to a new path, the copy has the original's self-pointer. First session in the copy triggers drift detection. The copy-detection branch (§7.2 step 3.a) handles this by checking whether the original path still exists with the original UID's self-pointer.

**Severity:** MEDIUM.
**Mitigation:** §7.2's algorithm. Document explicitly: copying a bootstrapped repo with `cp -r` is supported and yields a fresh registry entry on first session in the copy.

### 9.6 Concurrency — multiple sessions opening simultaneously

If two bootstrapped repos open Claude sessions at exactly the same moment and both detect drift, both write to the mailbox. The mailbox uses uid-named files (`<uid>.json`) so writes don't collide. main-apiary's processing is serialized via FileLock on the registry. No race.

**Severity:** LOW.
**Mitigation:** keep the existing FileLock contract on the registry. Mailbox files are atomic writes (`.tmp` + `os.replace`). Document: a second message from the same repo before the first is processed overwrites the first — that's intended (only the latest claim is meaningful).

### 9.7 Main-apiary unreachable at session open

If main-apiary is on an unmounted drive, deleted, or moved without cascade-fix running, the bootstrapped repo's hooks can't reach it.

**Severity:** MEDIUM.
**Mitigation:** §7.1 verification + §3.7-D32 loud warn + skip behavior. Hooks must always exit 0 (do not block the session). The user gets a clear printed message.

### 9.8 Dual-role for main-apiary

main-apiary is both "the apiary toolkit's source of truth" and "a bootstrapped repo." This dual role has subtle interactions:

- main-apiary's own `version.json` is what every other repo compares against. It must be kept current with main-apiary's actual code version.
- When migrations are added, main-apiary's own version is bumped first; then `apiary update` is run on each registered repo.
- main-apiary's self-pointer drift triggers cascade-fix (§7.3) — main-apiary is the only repo whose drift-handler does cascade-fix.

**Severity:** MEDIUM.
**Mitigation:** keep main-apiary's "I am the source of truth" handling in one place (probably a single function `is_main_apiary(repo_path)` that compares `repo_path` against the main-apiary path resolved from any pointer). The drift handler dispatches to either the regular-repo branch or the main-apiary branch based on this check.

### 9.9 Atomicity of generated files

`apiary install` writes ~10 files. If it crashes mid-install, the repo is in a partially-bootstrapped state.

**Severity:** MEDIUM.
**Mitigation:** install writes to a staging directory (`<repo>/.claude/.apiary-install-staging/`) and atomically swaps it in at the end (rename). On crash, the staging dir is left behind for inspection; the repo's prior state (or unbootstrapped state) is intact. `apiary doctor registry --fix` cleans up orphaned staging dirs.

### 9.10 Migration script crashes mid-chain

If `apiary update` runs `v0_3_to_v0_4` successfully then `v0_4_to_v0_5` crashes, the repo is at v0.4 partial. Version.json is bumped only after each migration's `upgrade()` returns successfully (§7.5 step 4.c).

**Severity:** MEDIUM.
**Mitigation:** each migration's `upgrade()` is responsible for its own atomicity (idempotent, transactional). The migration runner reverts version.json if a migration raises. Document this contract loudly in `<main-apiary>/migrations/README.md` (write that file as part of phase 0).

### 9.11 Windows path handling

The codebase has many Windows-specific learnings (UTF-8, path separators, drive letters). The new launcher and pointer files must use `pathlib.Path` end-to-end and avoid raw string concatenation. Self-pointer files store paths as strings; comparison must canonicalize via `Path(...).resolve()`.

**Severity:** MEDIUM.
**Mitigation:** add tests on Windows specifically. Add a test that creates a self-pointer with a Windows path (`D:\\Pro\\repo`), reads it back, and compares with `Path.cwd()`. Use `.resolve()` consistently.

### 9.12 GUI shortcut breaks when main-apiary moves

GUI launcher script lives at `<main-apiary>/gui/launch.bat` (or wherever). Desktop shortcuts pin to that path. When main-apiary moves, the shortcut breaks until manually re-pinned.

**Severity:** LOW (acceptable per D24).
**Mitigation:** document in user-facing release notes: "If you move the apiary checkout, re-pin your GUI shortcut to the new location." `apiary doctor pointers --fix` could optionally try to update Windows shortcuts (lnk files) — TBD.

### 9.13 Stranded global flag files (UX regression at migration time)

Today `~/.claude/budgeter-log-enabled` is set globally. Post-migration, each per-repo flag at `<repo>/.claude/apiary/flags/budgeter-log-enabled` starts disabled. User loses their setting.

**Severity:** MEDIUM (one-time UX hit).
**Mitigation:** the migration script (§10 phase 1) reads existing global flags and writes them to every bootstrapped repo's flags directory. This is a one-shot copy at migration time, not an ongoing fallback.

### 9.14 Stranded session history at migration time

Today `~/.claude/.session-history.json` has all sessions across all repos. Post-migration, each repo gets its own bounded history. The existing aggregate file's entries need to be split per repo by `cwd` field.

**Severity:** MEDIUM.
**Mitigation:** migration script reads `~/.claude/.session-history.json`, partitions entries by `cwd` matching each registered repo's `real_path`, writes the partitions into `<main-apiary>/.repos/<slug>/sessions/history.json`. Entries with cwds that don't match any registered repo are dropped (or archived to `<main-apiary>/.apiary/legacy/orphan-session-history.json` for one-time review).

### 9.15 Rip-out timing

If `setup.py --global` is removed before all 4 currently-bootstrapped repos are migrated, those repos lose all apiary functionality.

**Severity:** BLOCKER if mis-ordered.
**Mitigation:** §10's phased rollout enforces the ordering. Removal of global mode happens in phase 5, after all 4 repos are confirmed working under per-repo mode in phase 4.

---

## 10. Phased Rollout Plan

The migration must happen in this order. Each phase's exit criteria must be met before starting the next.

### Phase 0 — Preparation (no migration yet)

Outcome: tooling exists; no behavior change for users.

- [x] Promote `core/utils/state._allocate_next_id` to public `allocate_next_id`. Update callers.
- [x] Add `core/utils/state` helpers: `read_self_pointer`, `write_self_pointer`, `read_main_apiary_pointer`, `write_main_apiary_pointer`, `read_version`, `write_version`. Keep them under one module so future code finds them.
- [x] Create `<main-apiary>/migrations/` directory with a `README.md` documenting the migration contract. Write a no-op `v0_0_0_to_v0_1_0.py` example for shape reference.
- [x] Create `<main-apiary>/.apiary/forwarding/` mailbox directory. (`gui/` placeholder deferred to phase 3 when GUI state is migrated.)
- [x] Define main-apiary's current version as `0.1.0` in a new `<main-apiary>/VERSION` file (single-line semver). The startup hook reads this for version comparisons.
- [x] Implement `apiary doctor` skeleton with all subcommands listed in §7.6. Each subcommand is read-only initially (no `--fix` actions yet). — `core/doctor.py`
- [x] Verify all currently-bootstrapped repos (the 4 in `.repos/registry.json`) have correct entries with current `real_path`s. Run `apiary doctor unreachable` to confirm none are stale.
- [x] Update `core/utils/state.registry` schema to include `uid` and `version` fields. One-shot migration at `scripts/phase0_extend_registry.py` (idempotent; `--apply` to write).

Exit criteria:
- `apiary doctor` returns clean for all subsystems.
- All 4 registered repos have `uid` and `version` fields populated in registry.json.
- `<main-apiary>/VERSION` exists.
- All existing tests still pass.

### Phase 1 — Per-repo install path (parallel to global)

Outcome: `apiary install --target <repo>` works AND global install still works. Bootstrapped repos can run via either.

- [x] Implement `apiary install --target <repo>` (§7.8). — `core/install.py`
- [x] Implement the per-repo launcher template (§7.7). — `core/launcher_template.py`
- [x] Implement `apiary self-bootstrap` (§7.9). — `core/self_bootstrap.py`
- [x] Implement the per-repo PreToolUse drift-handler hook (§7.2). — `core/drift.py` + `core/hooks/per_repo_drift_check.py`
- [x] Implement mailbox processor (§7.4). — `core/mailbox.py` (`process_pending`)
- [x] Implement cascade-fix (§7.3). — `core/cascade.py`; wired into main-apiary's drift dispatch
- [x] Migrate per-repo flag handling: `core/flags.py` reads from `<repo>/.claude/apiary/flags/<name>-enabled` first, falls back to `~/.claude/<name>-enabled` if missing. (The fallback is ONLY for the duration of phases 1–4 — removed in phase 5.)
- [x] Migrate context-rules zone target: `scripts/install_context_rules.py` learns `--target <repo>` and writes to `<repo>/CLAUDE.md`. The `--global` flag continues to work for now.
- [x] Add `apiary uninstall --target <repo>` (§7.11). — `core/uninstall.py`
- [x] Unified `apiary` CLI (§14.6). — `core/cli.py` with subcommands install/uninstall/self-bootstrap/doctor/mailbox/cascade-fix/version

Exit criteria:
- `apiary self-bootstrap` from a fresh main-apiary clone produces a working main-apiary entry.
- `apiary install --target <some-test-repo>` produces a working bootstrapped repo whose hooks fire correctly.
- The bootstrapped test-repo's hooks invoke its own per-repo launcher (verifiable by adding a debug log line).
- Drift detection is testable: manually edit a self-pointer, open a session, observe a mailbox message gets queued.

### Phase 2 — Migrate the 4 existing bootstrapped repos one at a time

Outcome: each of `claude-apiary-1`, `HexMatCraft-2`, `HexWorld-3`, `HexWorld-5.7-4` is bootstrapped under the new model. Global install is still active for safety.

For each repo, in order (start with HexWorld-5.7-4 — least critical — and end with claude-apiary-1):

- [ ] Run `apiary install --target <repo>` from main-apiary.
- [ ] Verify per-repo files are present.
- [ ] Open a Claude session in the repo. Confirm hooks fire via the per-repo launcher (check session-tmp flag files appearing in `<repo>/.claude/apiary/session-tmp/` rather than `~/.claude/tmp/`).
- [ ] Confirm budgeter, scribe, and any tool the user uses there work correctly.
- [ ] Confirm session-history is being written to `<main-apiary>/.repos/<slug>/sessions/history.json`.
- [ ] Confirm CLAUDE.md zone exists in `<repo>/CLAUDE.md` with the expected rules.
- [ ] If anything fails, run `apiary uninstall --target <repo>` and investigate.

Exit criteria:
- All 4 repos bootstrapped under the new model.
- One full week of normal usage with no apiary regressions reported.

### Phase 3 — One-shot migration of state from global to per-repo / centralized

Outcome: data that lived in `~/.claude/` gets copied to its new home.

- [ ] Migrate flag files: read `~/.claude/{budgeter-log,budgeter-warn,budgeter-session-warn,auto-startup}-enabled`. For each one that exists, copy to every bootstrapped repo's `<repo>/.claude/apiary/flags/<name>-enabled`. (User can later toggle individual repos off.)
- [ ] Migrate session history: read `~/.claude/.session-history.json`. Partition entries by `cwd` matching each bootstrapped repo's `real_path`. Write partitions to `<main-apiary>/.repos/<slug>/sessions/history.json`. Archive orphan entries (those not matching any bootstrapped repo) to `<main-apiary>/.apiary/legacy/orphan-session-history.json`.
- [ ] Migrate transcript archives: move `~/.claude/transcripts/<sid>.jsonl` files into the appropriate `<main-apiary>/.repos/<slug>/sessions/transcripts/` based on the session's repo (look up sid → cwd from session-history).
- [ ] Migrate session-identity files: copy `~/.claude/.session-identity-<sid>.json` files into the right repo's `<main-apiary>/.repos/<slug>/sessions/identity-<sid>.json` based on the same lookup. Old session-identity files for sessions whose repo can't be determined are archived alongside orphan session-history.
- [ ] Migrate GUI state: copy `~/.claude/apiary_gui/*` to `<main-apiary>/.apiary/gui/`. Update `gui/paths.py` to read from the new location. Confirm the GUI starts and shows the same tabs/sidebar/theme.
- [ ] Update the apiary-managed zone in `~/.claude/CLAUDE.md` for each user — *delete the apiary zone only*, leaving the rest of `~/.claude/CLAUDE.md` intact. The per-repo zones in each `<repo>/CLAUDE.md` already exist from phase 1.

Exit criteria:
- Every per-repo data file exists in its new location and is readable.
- GUI starts and shows the same state as before.
- User's `~/.claude/CLAUDE.md` retains user-owned content; apiary-managed zone is gone.
- One week of usage confirms no functionality regression.

### Phase 4 — Full per-repo cutover

Outcome: hooks no longer rely on global install. Sessions in non-bootstrapped repos run as vanilla Claude.

- [ ] Switch each bootstrapped repo's `.claude/settings.json` to be the *only* place its hooks are registered. Remove apiary's hook entries from `~/.claude/settings.json` (use `is_apiary_entry()` from `core/hooks_lib.py`).
- [ ] Test: open a session in a bootstrapped repo — apiary hooks fire (per-repo). Open a session in a *non-bootstrapped* repo — no apiary hooks fire (vanilla session).
- [ ] Test: open a session in a bootstrapped repo with main-apiary's drive unmounted — loud warn message, vanilla session.
- [ ] Test: move a bootstrapped repo, open a session — drift handler runs, queues mailbox message.
- [ ] Test: move main-apiary itself, open a session in main-apiary — cascade-fix runs, all repos' main-apiary-pointers updated.
- [ ] Test: `cp -r` a bootstrapped repo, open a session in the copy — copy detection runs, new uid allocated.

Exit criteria:
- All migration tests pass.
- One week of usage with the global mode effectively dormant (entries removed from `~/.claude/settings.json`).

### Phase 5 — Rip out global mode

Outcome: nothing apiary-related lives at `~/.claude/`.

- [ ] Delete `~/.claude/apiary_launch.py`.
- [ ] Delete `~/.claude/apiary.json`.
- [ ] Delete `~/.claude/apiary_repos.json`.
- [ ] Delete `~/.claude/apiary_bootstrap.py`.
- [ ] Delete `~/.claude/.install-manifest.json`.
- [ ] Delete `~/.claude/apiary_gui/` and `~/.claude/apiary_gui_dev/`.
- [ ] Delete `~/.claude/{budgeter-log,budgeter-warn,budgeter-session-warn,auto-startup}-enabled`.
- [ ] Delete `~/.claude/commands/<apiary-files>.md` (use the manifest from before deletion as the source list).
- [ ] Delete `~/.claude/.session-history.json`, `~/.claude/.last-transcript.jsonl`, `~/.claude/transcripts/`.
- [ ] Delete `~/.claude/.session-identity-*` files.
- [ ] Remove the legacy/global fallback code paths added in phase 1 (per-repo flag fallback to global, etc.).
- [ ] Remove `setup.py --global` mode. Either delete the flag (with a clear error message) or remove `setup.py` entirely in favor of `apiary install`.
- [ ] Remove `APIARY_STATE_LAYOUT=legacy` escape hatch from `core/utils/state.py` and `core/apiary_launch.py`.
- [ ] Remove the `_global` install code paths in `setup.py` (`install_global_*`, `install_pre_commit_hook` for the apiary repo's own .git/hooks stays; that's not global mode).
- [ ] Update PORTABILITY.md, README.md, SETUP.md, all CLAUDE.md references, and every doc under `docs/` that mentions `--global`, `~/.claude/apiary*`, etc.
- [ ] Update `.gitignore` for any new entries that became obsolete.

Exit criteria:
- `ls ~/.claude/` shows only Claude Code's own files plus user-owned `CLAUDE.md`.
- `apiary doctor` (run from inside main-apiary) returns clean for all subsystems.
- All tests pass.
- All four bootstrapped repos still work normally.

### Phase 6 — Polish

- [ ] Update tests that mocked `Path.home() / ".claude"` for apiary state (§9.1 list).
- [ ] Update `incubator` tool to auto-bootstrap newly-spawned repos via `apiary install --target <new-repo>`.
- [ ] Add `apiary doctor` to the documentation index.
- [ ] Write a release-notes entry summarizing the migration.
- [ ] Delete this MIGRATION-PLAN.md file.

---

## 11. Verification & Acceptance Criteria

### 11.1 Functional verification

After phase 5, the following must all be true:

| Check | How to verify |
|---|---|
| `~/.claude/apiary*` is empty | `ls ~/.claude/apiary*` returns "No such file" |
| Hooks fire only in bootstrapped repos | Open Claude in `~/`; no apiary output. Open Claude in main-apiary; apiary startup banner appears. |
| Drift detection works | `mv <repo> <repo-renamed>`; open a session in `<repo-renamed>`; observe mailbox message at `<main-apiary>/.apiary/forwarding/<uid>.json`; open a session in main-apiary; observe registry updated. |
| Copy detection works | `cp -r <repo> <repo-copy>`; open a session in `<repo-copy>`; observe new UID allocated; both repos remain functional. |
| Cascade fix works | `mv <main-apiary> <main-apiary-renamed>`; open a session in main-apiary-renamed; observe all bootstrapped repos' `main-apiary-pointer.json` updated. |
| Version migration works | Bump main-apiary to v0.2.0; run `apiary update` in a bootstrapped repo; observe version.json updated and any v0.1→v0.2 migration script ran. |
| Doctor surfaces drift | Manually corrupt a registry entry's path; run `apiary doctor unreachable`; observe report. |
| Mailbox dedup works | Move a repo twice in a row before main-apiary processes; observe only one mailbox file (latest) and registry ends up at the latest path. |
| Loud warn on missing main-apiary | `mv <main-apiary> /tmp/elsewhere`; open a session in a bootstrapped repo; observe printed warn message and that the session continues normally as a vanilla Claude session. |
| GUI works after move | Move main-apiary; relaunch GUI from new path; tabs/sidebar/theme intact. |
| Existing tests pass | `pytest` exits 0. |

### 11.2 Non-regression checks

| Subsystem | Verify |
|---|---|
| Scribe | `python ~/.claude/apiary_launch.py scribe/notes.py list` (or its successor) still lists notes for current repo. |
| Budgeter | Cost tracking still appends to per-target state. Toggle flags work. |
| Compass | Backfill from `~/.claude/projects/<key>/` still finds transcripts and produces observations. |
| Runner | Stage subprocesses still work (APIARY_RUNNER_SUBPROCESS env var still skips appropriate hooks). |
| GUI | Tabs persist across restart. Theme hot-reload works. Permission MCP still bridges correctly. |
| Slash commands | `/wrapup`, `/note`, `/notes`, `/budgeter-log`, `/apiary-context`, etc., all work in any bootstrapped repo. |
| Pre-commit hook | `git commit` in main-apiary still runs `docs/check.py`. |

### 11.3 Edge cases to cover in tests

- Bootstrap a repo, open a session, verify session-tmp files appear in per-repo session-tmp dir.
- Bootstrap, move, bootstrap again — should be idempotent (re-registers same repo, doesn't duplicate uid).
- Bootstrap on a directory that's not a git repo — should error clearly (`core/utils/state.resolve_target_state_dir` already enforces this; verify error path).
- `apiary install` mid-run kill (Ctrl-C) — staging dir is cleaned up by `apiary doctor registry --fix`.
- Two `apiary install` runs racing — second waits on FileLock, second's outcome is the final state.

---

## 12. Where the Explorer Agent's Report Disagrees with This Plan

The risk-mapping subagent run during the design conversation produced a thorough report but reached several conclusions that conflict with this plan's decisions. Don't be confused by this — the agent didn't have full context on the decisions made in the conversation. **This plan wins.** Specifically:

| Agent claim | Why this plan disagrees |
|---|---|
| "Session history MUST stay global at `~/.claude/.session-history.json`" | The agent treated this file as Claude Code infrastructure. It's not — apiary writes it (`core/hooks/save_transcript.py`), apiary owns the schema, apiary owns the consumers. Per D15, it moves to `<main-apiary>/.repos/<slug>/sessions/history.json` per-repo. |
| "Session identity MUST stay global at `~/.claude/.session-identity-<sid>`" | Same: apiary writes this (`core/hooks/inject_session.py`, `core/session.py`). Per D16, it moves to `<main-apiary>/.repos/<slug>/sessions/identity-<sid>.json`. The session_id ITSELF is global (Claude Code generates it), but where apiary stores its own role/mission metadata for that session is apiary's choice. |
| "GUI state stays at `~/.claude/apiary_gui/`" | Per D22, GUI state moves to `<main-apiary>/.apiary/gui/`. The GUI is a single-process per-user app, but its state files belong with main-apiary's other state, not at a global location. |
| "`~/.claude/apiary.json` pointer stays unchanged" | Per D25–D27, the global pointer is removed entirely. Each bootstrapped repo has its own `main-apiary-pointer.json`. There is no global pointer post-migration. |
| "`~/.claude/apiary_launch.py` launcher stays unchanged" | Same: per D26, the global launcher is removed. Each bootstrapped repo has its own launcher under `<repo>/.claude/apiary/launch.py`. |
| "Add fallback to global flags for backward compat after migration" | Per D25, no backward compat. The migration's phase 1 includes a temporary fallback for the duration of phases 1–4 only; phase 5 removes it entirely. |
| "Session-tmp flag files (`~/.claude/tmp/<sid>_*`) must stay global because session_id is global" | These are apiary-owned per-session ephemeral files. They move to `<repo>/.claude/apiary/session-tmp/`. Per-repo is fine because each Claude Code session opens in one cwd; session-tmp goes there. |

The agent's correct findings (which this plan absorbs):

- Compass reads from `~/.claude/projects/<key>/` — that path is owned by Claude Code; apiary continues to read from it (§8.1).
- Tests pinned to global paths need updating (§9.1).
- Project-key derivation has legacy/stable variants — preserve existing fallback logic (§9.3).
- Pre-existing in-repo legacy state (some repos have `<repo>/.apiary/scribe/`) needs migration (§9.2).
- Atomic write patterns (FileLock, `.tmp` + `os.replace`) for registry — keep them (§9.6).
- Windows path handling — use pathlib end-to-end (§9.11).
- Dual-role complexity for main-apiary — handled in §9.8.
- Stranded global state at migration time (flags, history) — addressed in phase 3 (§9.13–9.14).

---

## 13. Subtle Things — Don't Forget

Easy-to-miss details that the next session needs to keep in mind:

### 13.1 main-apiary IS a bootstrapped repo

It has its own `<main-apiary>/.claude/apiary/{launch.py, main-apiary-pointer.json, self-pointer.json, version.json}`. Its `main-apiary-pointer.json` points to *itself*. This is correct — it's how the unified drift-detection logic applies uniformly. main-apiary's UID is 1 (already true today).

### 13.2 The cascade-fix is the only place main-apiary writes to other repos' files

In every other code path, main-apiary only reads from bootstrapped repos. The cascade-fix specifically rewrites every registered repo's `main-apiary-pointer.json` when main-apiary itself moves. This is the one bidirectional flow.

### 13.3 The mailbox is single-consumer

Only main-apiary processes the mailbox. Multiple bootstrapped repos can produce messages (one file per repo). main-apiary processes them serially under FileLock. There is no risk of two main-apiary instances racing — there is only one main-apiary.

### 13.4 self-pointer drift is checked on every session

The PreToolUse hook (or equivalent first-firing hook) checks self-pointer on first tool call of every session. The check is cheap (one file read + one path comparison). The drift case is rare and triggers the algorithm in §7.2.

### 13.5 The `name` field

`name` (the slug-name part of `<name>-<uid>` folder names) is set at first bootstrap from the repo's directory basename. It does NOT change on move. If you `mv my-cool-repo somewhere/else`, the slug stays `my-cool-repo-<uid>`. The user can manually rename via `apiary rename <new-name>` (TBD §14.4).

### 13.6 `version.json` is local to a clone

If you have main-apiary on machine A at v0.4 and machine B at v0.5, and you clone a bootstrapped repo from A to B, the clone's `version.json` shouldn't be in git (it's per-clone state, gitignored). On first session in the clone, the version-mismatch check fires and prompts `apiary update`.

### 13.7 Per-repo CLAUDE.md interaction with user-owned content

`<repo>/CLAUDE.md` may contain user-owned project rules around the apiary-managed zone. The apiary zone is bounded by `<!-- apiary-context-rules-start -->` / `<!-- apiary-context-rules-end -->` sentinels (already defined in `core/context_rules.py`). `apiary install` writes/updates only the zone, never the surrounding content.

### 13.8 Don't rename `_allocate_next_id` in a vacuum

It's called from at least:
- `core/utils/state.resolve_target_state_dir` (current sole caller)

Future callers added by this migration:
- `apiary install --target <repo>` (for new bootstraps)
- The drift handler's copy-detection branch (for `register_copy` mailbox messages)

All three must use `allocate_next_id` — never reimplement.

### 13.9 The launcher must always exit 0 in the failure case

If main-apiary is unreachable, the per-repo launcher prints a warning and exits 0 (success), so the hook doesn't block the session. Hooks that exit non-zero block tool calls. The launcher's job in the failure case is to be invisible (let Claude proceed as if no hook were registered).

### 13.10 The pre-commit hook in `<main-apiary>/.git/hooks/pre-commit` is unrelated to this migration

That hook runs `docs/check.py` before commits to main-apiary. It is installed once during apiary's own setup (a different thing from `apiary install --target`). It stays.

### 13.11 The `incubator` tool

`incubator/cli.py` spawns a new side-project repo. After this migration, it must auto-run `apiary install --target <new-repo>` after creating the skeleton, otherwise the new repo has no apiary integration. (Phase 6 task.)

### 13.12 `~/.claude/projects/<project-key>/` is read-only from apiary's POV

Compass reads transcripts from there. Apiary never writes there — that's Claude Code's own directory. If Claude Code's project-key derivation logic changes (it has changed before — see learnings L-2026-41, L-2026-97), apiary's read paths must be updated, but that's an issue separate from this migration.

### 13.13 The `name` field disambiguation problem

If two repos both have basename `myproject`, they'll both want slug `myproject-<uid>`. The UIDs differ so the slugs are distinct (`myproject-7`, `myproject-12`), but the user-facing name is duplicated. Acceptable — the UID disambiguates. If the user wants distinct names they can `apiary rename` (TBD §14.4).

### 13.14 Don't conflate "per-repo" and "in-repo"

Per-target *data* is per-repo conceptually but stored centrally in main-apiary at `.repos/<slug>/`. Per-repo *install state* (settings.json, launcher, pointers) IS in the repo at `<repo>/.claude/apiary/`. The boundary is intentional — moving data files between machines requires re-bootstrapping anyway, so centralizing them in main-apiary loses nothing.

---

## 14. Open Questions for the Next Session

These were identified during design but not resolved. Surface them to the user in the next session before implementing the affected pieces.

### 14.1 Auto-run vs prompt-only for `apiary update` on version mismatch

Today's plan: on version mismatch, print a one-line message in the session telling the user to run `apiary update` manually.

Alternatives:
- Auto-run on first session after detecting mismatch (could be intrusive if migration is lengthy).
- Auto-run only for "patch" version bumps; prompt for minor/major.

Recommendation: prompt-only initially. Promote to auto-run later if user finds it tedious.

### 14.2 `update.py` rollback semantics

If migration v0.4→v0.5 fails partway, do we leave the repo at v0.4 or attempt to roll back files modified by the partial v0.4→v0.5 run? Each migration's `upgrade()` is supposed to be idempotent and transactional (per §7.5), but enforcing that is the migration author's responsibility.

Open question: does `apiary update` provide a `safety/` snapshot mechanism (copy-before-modify) so it can roll back automatically? Or trust migration authors to implement transactional upgrades themselves?

Recommendation: trust migration authors but provide a helper in `<main-apiary>/migrations/_lib.py` for "snapshot file before mutating, restore on raise."

### 14.3 GUI state migration timing

GUI state moves from `~/.claude/apiary_gui/` to `<main-apiary>/.apiary/gui/` in phase 3. But the GUI is a long-running process. If the user has the GUI open during migration, the move could corrupt state.

Recommendation: phase 3's GUI migration step must be done with the GUI process fully shut down. Add a pre-check that refuses to migrate if `permission_mcp.log` shows recent activity.

### 14.4 `apiary rename <new-name>` for changing a repo's slug

Not in scope for the initial migration but worth a stub: do we want a CLI command to rename a registered repo's slug? Useful when two repos both happen to share a basename.

Recommendation: out of scope for the initial migration. Add later if needed.

### 14.5 Fate of `setup.py`

After phase 5, what happens to the existing `setup.py`?

Options:
- Delete it entirely; `apiary install` is the only entry point.
- Keep it as a thin wrapper that errors with a clear "run `apiary install --target <repo>` instead" message.
- Repurpose it as the orchestrator for `apiary self-bootstrap` + `apiary install`.

Recommendation: thin wrapper that prints the migration message and exits 1, so users following old docs get a clear redirect.

### 14.6 What `apiary` itself looks like as a CLI

The plan refers to `apiary install`, `apiary update`, `apiary doctor`, `apiary self-bootstrap`. Today there's no top-level `apiary` CLI — there's `core/apiary_bootstrap.py`, `setup.py`, etc.

Open question: do we add a unified `apiary` entry point (`<main-apiary>/apiary` or `core/cli.py` exposing an `apiary` console_script in pyproject.toml)? Or keep things as separate `python ...py` invocations?

Recommendation: add a unified `apiary` CLI as part of phase 1. Sub-commands: `install`, `uninstall`, `update`, `doctor`, `self-bootstrap`, `version`. Reduces user-facing confusion and gives a place to dispatch from.

### 14.7 Disambiguating "main-apiary" when it isn't current cwd

The per-repo launcher reads `main-apiary-pointer.json` to find main-apiary. But sometimes a bootstrapped repo's hooks need to invoke `apiary` CLI commands that target main-apiary itself (e.g. mailbox processor needs to write to `<main-apiary>/.apiary/forwarding/`). These work via the pointer.

But the `apiary` CLI invoked from inside a bootstrapped repo (not main-apiary) — how does it find main-apiary? Via the same pointer.

Open question: should `apiary` CLI also support `--main-apiary <path>` for the case where a user wants to target a specific main-apiary?

Recommendation: yes, as an override. Default is via pointer.

### 14.8 Test isolation for the new model

How do tests run in CI? Today some tests mock `Path.home()` to point at a tmpdir. After migration, tests need to mock both a fake "main-apiary" tmpdir AND a fake "bootstrapped repo" tmpdir. This is feasible but more boilerplate.

Recommendation: add a `core/utils/test_helpers.py` with a context manager that sets up both fake-main-apiary and fake-target-repo in tmpdirs and restores afterward. Reuse across all test files.

---

## 15. Quick-Start for the Next Session

When you (the next session) start work on this:

1. Re-read this entire file. Don't skim.
2. Verify the working state matches what this plan assumes. Run:
   - `git status` (should be clean or near-clean on `master`)
   - `cat <main-apiary>/.repos/registry.json` (4 entries: claude-apiary-1, HexMatCraft-2, HexWorld-3, HexWorld-5.7-4)
   - `ls ~/.claude/apiary*` (should show the global files this plan removes)
   - `ls <main-apiary>/.apiary/` (probably doesn't exist yet — phase 0 creates it)
3. Confirm with the user that nothing has changed since this plan was written. Ask: "Has anything changed in the apiary toolkit or in any bootstrapped repo since 2026-05-05? Any new repos added or removed?"
4. If anything has changed, surface it to the user before starting.
5. Begin with phase 0 of §10. Get the user's confirmation before each phase transition.
6. Treat §14 as questions to ask the user — don't silently invent answers.
7. If you encounter a situation not covered by this plan, **stop and ask the user**. The plan is comprehensive but cannot anticipate every edge case. Don't improvise; surface the gap.
8. Update this file as you work (mark phase/step checkboxes, note issues encountered, log decisions made). When all phases complete, delete the file.

---

*End of plan.*
