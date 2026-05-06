---
type: reference
title: CLI Tools
scope: project
description: All Python CLI entry points with subcommands, flags, and usage examples
framework_version: "1.0"
last_verified: "2026-04-23"
---

# CLI Tools

Every script below is invoked as `python <path> [subcommand] [flags]`. No external dependencies — stdlib only.

## scribe/notes.py

Core note and learning management.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `add` | `notes.py add --type <type> --content "<text>"` | Add a note |
| `list` | `notes.py list [filters]` | List active notes |
| `get` | `notes.py get <ID>` | Show a single note by ID (e.g. `T-2026-1`) |
| `done` | `notes.py done <ID>` | Mark a note as done (e.g. `T-2026-1`) |
| `defer` | `notes.py defer <ID>` | Hide from default listings and startup banner without closing. Use when revisiting needs more data. |
| `resume` | `notes.py resume <ID>` | Undo a defer — return the note to active |
| `update` | `notes.py update <ID> --content "<text>"` | Update note content (e.g. `T-2026-1`) |
| `archive` | `notes.py archive [--before YYYY-MM-DD]` | Archive old notes |
| `learn` | `notes.py learn --content "<text>"` | Add a learning |
| `learnings` | `notes.py learnings` | List all learnings |
| `unlearn` | `notes.py unlearn <ID>` | Remove a learning (e.g. `L-2026-3`) |
| `handoff-sessions` | `notes.py handoff-sessions` | List sessions with handoffs |
| `migrate` | `notes.py migrate` | Run data migrations |

> **Note IDs** use TYPE-YEAR-seq format (e.g. `T-2026-1`, `L-2026-3`). Legacy bare integers are accepted via migration lookup. See `scribe/CLAUDE.md` for the full prefix table.

### Common flags

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--project PROJECT` | all | Project key override (default: derived from cwd) |
| `--type TYPE` | add, list | Note type: `todo`, `handoff`, `decision`, `wishlist`, `reference`, `blocker`, `context`, `general`, `learning` |
| `--content TEXT` | add, update, learn | Note/learning content |
| `--session-id ID` | add, update, learn | Associate with a session |
| `--auto` | add | Mark as auto-generated |
| `--if-no-handoff-for ID` | add | Only add if no handoff exists for this session |
| `--full` | learnings | Print full content (not truncated) |
| `--search TEXT` | list, learnings | Full-text search |
| `--last N` / `--limit N` | list | Show last N notes (both spellings accepted as aliases) |
| `--all` | list | Include done, dropped, and deferred notes |
| `--deferred` | list | Show only deferred notes |
| `--archive` | list | Search archive instead of active |
| `--role ROLE` | add, list, learn | Session role filter |
| `--mission MISSION` | add, list, learn | Session mission filter |
| `--before DATE` | archive | Archive notes before this date |

## scribe/backup_indexes.py

Snapshot the scribe v2 indexes (all type folders plus learnings, active and archive) to a dated backup directory under `<state-dir>/backups/<YYYY-MM-DD>/`. Runs a retention prune after each backup so only the N most recent snapshots remain.

```bash
python scribe/backup_indexes.py
python scribe/backup_indexes.py --retain 14
python scribe/backup_indexes.py --project other-project
```

| Flag | Required | Description |
|------|----------|-------------|
| `--retain N` | no | Number of dated backups to keep (default 30; `0` keeps only the newest) |
| `--project KEY` | no | Project key override (defaults to the current repo's scribe state dir) |

Copies `index.jsonl` files and the global `next_id` counter — the `.md` bodies are not backed up, since they are append-only and tracked separately. Intended to run on a daily cron; exits 0 even when no state dir exists (the first `/apiary-context` call will create one).

## core/startup.py

Session initialization and summary loading.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `init` | `startup.py init --session-id ID --first-message "..." --repo-dir PATH` | Register the session and persist its identity |
| `summary` | `startup.py summary [--repo-dir PATH] [--role R] [--mission M]` | Load active notes and learnings summary |

### Flags

| Flag | Applies to | Required | Description |
|------|-----------|----------|-------------|
| `--session-id ID` | init | yes | Session ID |
| `--first-message TEXT` | init | yes | User's first message |
| `--repo-dir PATH` | init, summary | init: yes | Repository root directory |
| `--role ROLE` | summary | no | Filter by role (default: `user`) |
| `--mission MISSION` | summary | no | Filter by mission (default: `general`) |

## core/doctor.py

Read-only consistency checks for the per-repo install model. Phase-0 scaffold;
`--fix` actions land in later phases. See `MIGRATION-PLAN.md` §7.6.

```bash
python core/doctor.py [subcommand] [--apiary-repo PATH]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| (none) | Run all checks, print a summary |
| `pointers` | Verify main-apiary's self-pointer matches its actual location |
| `registry` | Walk every registered repo: path exists; uid/version fields present |
| `mailbox` | Count pending forwarding messages at `<apiary>/.apiary/forwarding/` |
| `versions` | Compare each repo's pinned version against `<apiary>/VERSION` |
| `orphans` | Folders under `.repos/<slug>/` whose UID has no registry entry |
| `duplicates` | Registry entries sharing a `real_path` |
| `unreachable` | Registry entries whose `real_path` does not exist on disk |

### Flags

| Flag | Description |
|------|-------------|
| `--apiary-repo PATH` | Path to main-apiary checkout (default: resolved via launcher / pointer) |

### Exit code

- `0` — all checks pass (notes are informational and do not fail the run).
- `1` — any check reported an issue.

## budgeter/report.py

Usage reporting CLI.

```bash
python budgeter/report.py [options]
```

| Flag | Description |
|------|-------------|
| `--date YYYY-MM-DD` | Show only entries from this date |
| `--since YYYY-MM-DD` | Show entries from this date onwards |
| `--flat` | Flat chronological list (no grouping) |
| `--grouped` | Group by session only (no task breakdown) |
| `--by-turn` | Group by session > task (default) |
| `--all` | Include zero-delta entries |
| `--by-agent` | Show per-agent-type token breakdown |
| `--by-request` | Group by `request_id` (sums multi-call chains like one runner run; entries without a `request_id` bucket into `(no request)`) |
| `--weighted` | Weight tokens by type: cache 0.1x, output 5x |
| `--feedback` | Show warning precision and rule breakdown |

## budgeter/tune.py

Suggest rule weight adjustments based on historical data.

```bash
python budgeter/tune.py [options]
```

| Flag | Description |
|------|-------------|
| `--min N` | Min samples per rule before suggesting (default: 5) |
| `--percentile N` | Expensive threshold percentile (default: from config) |
| `--yes` | Apply changes without confirmation prompt |

## budgeter/query_request.py

Sum total tokens logged for a given `request_id` from the budgeter log. Used by `/harden` and other multi-call flows to query their running spend for the current request.

```bash
python budgeter/query_request.py --request-id <rid> [--cwd <project_dir>]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--request-id ID` | yes | The `APIARY_REQUEST_ID` value to query |
| `--cwd DIR` | no | Project working directory (selects per-project log via `logger.configure_for_project`) |

Prints the total token count (single integer) to stdout on success. Exits non-zero with the error message on stderr if the cwd is invalid or the query fails. Intended for use in Bash pipelines where the caller captures stdout and checks the exit status.

## budgeter/log_agent_cost.py

Log background agent token costs. Reads `<usage>` XML from stdin.

```bash
echo '<usage>...</usage>' | python budgeter/log_agent_cost.py --session-id ID [--agent NAME] [--cwd DIR] [--request-id ID]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--session-id ID` | yes | Current session ID |
| `--agent NAME` | no | Agent name (e.g. "startup") |
| `--cwd DIR` | no | Working directory for config resolution |
| `--request-id ID` | no | Optional grouping id for multi-call chains (e.g. one runner run). Surfaces in `report.py --by-request`. |

## compass/observations.py

Inspect and maintain per-session personality observation files at `<state-dir>/compass/observations/` (per-target state dir resolved via the registry; see [File Storage](file-storage.md)).

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `count` | `observations.py count` | Print active observation count |
| `list` | `observations.py list [--full] [--archive]` | List observation files (one per line; `--full` prints JSON; `--archive` lists archived files instead) |
| `validate` | `observations.py validate <path> [--no-filename-check]` | Validate one observation file's schema. Default checks `session_id` matches the filename stem |
| `archive` | `observations.py archive [--apply]` | Archive sweep — moves files older than 90 days into `observations/archive/<iso-year>-<iso-week>/`. Skips entirely when active count is below 50. Dry-run by default; `--apply` performs the move |

## compass/synthesize.py

Read active observations, previous `personality.md`, and `corrections.md`; call headless `claude -p` to produce a new `personality.md`. Used by `/compass-sync` and the weekly cron entry.

```bash
python ~/.claude/apiary_launch.py compass/synthesize.py
python ~/.claude/apiary_launch.py compass/synthesize.py --dry-run
python -m compass.synthesize --cron        # cron-driven; no-ops if personality.md is < 7 days old
```

| Flag | Required | Description |
|------|----------|-------------|
| `--dry-run` | no | Print the synthesis prompt instead of calling claude |
| `--model MODEL` | no | Override the claude CLI's default model |
| `--cron` | no | Self-throttle to a 7-day cadence (no-op if `personality.md` was rewritten in the last week) |

Exit codes: `0` wrote `personality.md`; `1` no active observations; `2` claude subprocess failed (previous file untouched).

## compass/backfill.py

Extract observations from historical session transcripts via headless claude. Selectors are combinable and intersected.

```bash
python ~/.claude/apiary_launch.py compass/backfill.py --last 5
python ~/.claude/apiary_launch.py compass/backfill.py --session-ids 1089da5c,8123e697
python ~/.claude/apiary_launch.py compass/backfill.py --since 2026-04-10 --last 5
```

| Flag | Required | Description |
|------|----------|-------------|
| `--last N` | one of these | N most recent transcripts by mtime |
| `--session-ids LIST` | one of these | Comma-separated 8-char prefixes or full UUIDs |
| `--since YYYY-MM-DD` | one of these | Only transcripts modified on/after this date |
| `--force` | no | Overwrite existing observation files (default: skip) |
| `--model MODEL` | no | Override the claude CLI's default model |

Exit codes: `0` at least one file written; `1` no selectors / no matches / nothing written; `2` claude subprocess failed for every selected session.

## incubator/cli.py

Spawn a new side-project repo wired up with the apiary toolkit. Used by the `/incubator` skill after `/refine` produces a spec note. Lays down a Python+poetry skeleton, runs `git init`, and migrates the spec into the new repo's scribe.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `spawn` | `cli.py spawn --path <abs-path> --spec-note-id <id> [--author "<name>"] [--session-id ID]` | Create the new repo and migrate the spec |

### Flags

| Flag | Required | Description |
|------|----------|-------------|
| `--path` | yes | Absolute target directory; must not exist; parent must exist; must not be inside an existing git repo |
| `--spec-note-id` | yes | ID of the `/refine` context note in apiary scribe (e.g. `C-2026-43`) |
| `--author` | no | Author string for `pyproject.toml`; defaults to git config `user.name <user.email>` |
| `--session-id` | no | Optional session ID stamped on the migrated spec note |

Exit codes: `0` success; `2` validation error (bad path); `3` spec note not found; `4` spawn failure (rolled back automatically); `5` partial success — repo created but spec migration failed (recover manually).

Templates that get written into the new repo live under `incubator/templates/` (`gitignore.tmpl`, `pyproject.toml.tmpl`, `CLAUDE.md.tmpl`).

## refiner/round_counter.py

Track refinement round counts per session. Used by the `/refine` skill to enforce the 15-round soft limit.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `start` | `round_counter.py start --session-id ID` | Initialize counter at 0 for this session |
| `tick` | `round_counter.py tick --session-id ID` | Increment by 1, print new count |
| `reset` | `round_counter.py reset --session-id ID` | Reset to 0 |
| `status` | `round_counter.py status --session-id ID` | Print current count without incrementing |

### Flags

| Flag | Required | Description |
|------|----------|-------------|
| `--session-id ID` | yes | Session ID used to scope the counter file |

State is stored at `refiner/tmp/round_<session-id>.json`. Directory is auto-created on first write.

## researcher/cli.py

Manage structured research findings per repo. Entries live at `<state-dir>/research/<topic>/<slug>.md` with a YAML-subset frontmatter (title, topic, tags, dates, sources) and a standard body (Summary / Context / Findings / Code / Caveats). A controlled tag vocabulary lives at `<state-dir>/research/tags.yaml`. See [File Storage](file-storage.md) for the per-target state-dir resolver.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `add` | `cli.py add <topic> "<title>" [--tags t1,t2,...]` | Scaffold a new entry from the template. Rejects unknown tags and duplicate slugs |
| `find` | `cli.py find <query> [--limit N]` | Ranked search (title ×3, tags ×2, content ×1). Exits 0 even on zero hits |
| `list` | `cli.py list [--topic X] [--tag Y]` | List entries grouped by topic, optionally filtered |
| `show` | `cli.py show <topic> <slug>` | Print the full entry file to stdout |
| `verify` | `cli.py verify <topic> <slug>` | Bump `date_last_verified` to today |
| `register-tag` | `cli.py register-tag <tag>` | Append a tag to `tags.yaml` (controlled vocabulary) |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success (also returned by `find` with zero hits) |
| `2` | Validation error: unknown tag, duplicate slug, entry not found, tag already registered |
| `3` | Config error: invalid YAML in `tags.yaml` or entry frontmatter |

State is auto-created on first `add` or `register-tag`: `<state-dir>/research/` directory and a default `tags.yaml` with empty tag list.

## captures/cli.py

Manage visual captures (screenshots, UI mockups, viewport shots, etc.) per repo. Each capture pairs an image file with a markdown sidecar that holds metadata. State lives at `<state-dir>/captures/<topic>/<slug>.<ext>` (image) alongside `<topic>/<slug>.md` (sidecar, YAML-subset frontmatter: title, topic, tags, captured_at, image, session_id, related_notes, sources, plus a free-text context body). A controlled tag vocabulary lives at `<state-dir>/captures/tags.yaml`. See [File Storage](file-storage.md) for the per-target state-dir resolver.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `add` | `cli.py add <topic> <image-path> --title "<t>" [--tags t1,t2] [--context "<text>"] [--related ID1,ID2] [--session-id <id>] [--move]` | Ingest an image by copying (default) or moving it into the store; writes the sidecar. Rejects unknown tags, unsupported extensions, duplicate slugs |
| `find` | `cli.py find <query> [--limit N]` | Ranked search over sidecar title/tags/body (title ×3, tags ×2, content ×1). Exits 0 even on zero hits |
| `list` | `cli.py list [--topic X] [--tag Y]` | List captures grouped by topic, optionally filtered |
| `show` | `cli.py show <topic> <slug>` | Print the sidecar contents followed by the absolute image path |
| `path` | `cli.py path <topic> <slug>` | Print only the absolute image path (for scripting / feeding into Claude's Read tool) |
| `register-tag` | `cli.py register-tag <tag>` | Append a tag to `tags.yaml` (controlled vocabulary) |

### Allowed image extensions

`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp` (case-insensitive).

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success (also returned by `find` with zero hits) |
| `2` | Validation error: unknown tag, duplicate slug, entry not found, unsupported extension, missing source image, tag already registered |
| `3` | Config error: invalid YAML in `tags.yaml` or sidecar frontmatter |

State is auto-created on first `add` or `register-tag`: `<state-dir>/captures/` directory and a default `tags.yaml` with empty tag list.

## harden/validate_and_assign.py

Combined validate + assign-IDs script. Preferred over calling validators and assign_ids separately.

```bash
echo '<json>' | python harden/validate_and_assign.py findings [--check-files] [--deep] [--sanitize]
python harden/validate_and_assign.py findings --file findings.json [--check-files] [--deep] [--sanitize]
python harden/validate_and_assign.py response --file response.json --expected-ids ATK-001,ATK-002 [--check-files]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `findings` | Validate and assign ATK-IDs to Attacker findings |
| `response` | Validate and assign DEF-IDs to Defender response |

### Flags

| Flag | Applies to | Required | Description |
|------|-----------|----------|-------------|
| `--file PATH` | both | no | Read JSON from file instead of stdin |
| `--check-files` | both | no | Verify referenced files exist (code mode) |
| `--deep` | findings | no | Require Given/When/Then scenarios |
| `--sanitize` | findings | no | Auto-fix common issues (strip unknown fields, map invalid categories) |
| `--expected-ids IDS` | response | yes | Comma-separated ATK-IDs that must be addressed |

Exit 0 + validated JSON with IDs on success. Exit 1 + error details on failure.

## harden/assign_ids.py

Assign deterministic sequential IDs to harden agent output. Reads JSON array from stdin or file.

```bash
echo '<json_array>' | python harden/assign_ids.py --prefix ATK
python harden/assign_ids.py --prefix ATK --file findings.json
```

| Flag | Required | Description |
|------|----------|-------------|
| `--prefix PREFIX` | yes | ID prefix: `ATK` (findings) or `DEF` (responses) |
| `--file PATH` | no | Read JSON from file instead of stdin |

## harden/validate_findings.py

Validate Attacker output structure. Reads JSON from stdin or file.

```bash
echo '<json>' | python harden/validate_findings.py [--check-files] [--deep] [--sanitize]
python harden/validate_findings.py --file findings.json [--check-files] [--deep] [--sanitize]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--check-files` | no | Verify referenced files exist (code mode) |
| `--deep` | no | Require Given/When/Then scenarios |
| `--file PATH` | no | Read JSON from file instead of stdin |
| `--sanitize` | no | Auto-fix common issues before validation |

Exit 0 + validated JSON on success. Exit 1 + error details on failure.

## harden/validate_response.py

Validate Defender output structure. Reads JSON from stdin or file.

```bash
echo '<json>' | python harden/validate_response.py --expected-ids ATK-001,ATK-002 [--check-files]
python harden/validate_response.py --file response.json --expected-ids ATK-001,ATK-002 [--check-files]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--expected-ids IDS` | yes | Comma-separated ATK-IDs that must be addressed |
| `--check-files` | no | Verify referenced files exist (code mode) |
| `--file PATH` | no | Read JSON from file instead of stdin |

Exit 0 + validated JSON on success. Exit 1 + error details on failure.

## harden/round_counter.py

Track harden round counts per session. Same interface as `refiner/round_counter.py`.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `start` | `round_counter.py start --session-id ID` | Initialize counter at 0 |
| `tick` | `round_counter.py tick --session-id ID` | Increment by 1, print new count |
| `reset` | `round_counter.py reset --session-id ID` | Reset to 0 |
| `status` | `round_counter.py status --session-id ID` | Print current count without incrementing |
| `defender` | `round_counter.py defender --session-id ID --set AGENT_ID` | Store defender agent ID |
| `defender` | `round_counter.py defender --session-id ID --get` | Retrieve defender agent ID (exit 1 if not set) |

State is stored at `harden/tmp/round_<session-id>.json`. Format: `{"session_id": "...", "count": N, "defender_agent_id": "..."}`.

## setup.py

Unified installer for all tools.

```bash
python setup.py --global [--with-test-suite]
python setup.py --project-path /path/to/project
python setup.py --check
```

| Flag | Description |
|------|-------------|
| `--global` | Install globally in `~/.claude/settings.json` |
| `--project-path PATH` | Install budgeter hooks for a specific project |
| `--check` | Validate installation without making changes |

## scripts/install_context_rules.py

Install / sync / audit shareable context-rules into `~/.claude/CLAUDE.md`. Owns a marked managed zone wrapped in `<!-- apiary-context-rules-start --> ... <!-- apiary-context-rules-end -->` sentinels — content outside that zone is never touched. Hand-edits inside the zone are detected as tampering and require `--force`.

```bash
python scripts/install_context_rules.py                # interactive y/n/v per rule
python scripts/install_context_rules.py --list
python scripts/install_context_rules.py --install-all
python scripts/install_context_rules.py --install <id>...
python scripts/install_context_rules.py --install-category behavioral
python scripts/install_context_rules.py --uninstall <id>...
python scripts/install_context_rules.py --sync
python scripts/install_context_rules.py --check
python scripts/install_context_rules.py --diff <id>
```

| Flag | Description |
|------|-------------|
| `--list` | List all rules with installed/out-of-date/tampered status |
| `--install ID...` | Install specific rule ids |
| `--install-all` | Install every rule under `context-rules/` |
| `--install-category CAT` | Install all rules in a category (e.g. `behavioral`) |
| `--uninstall ID...` | Remove specific rule ids; leaves outer zone intact |
| `--sync` | Re-render currently-installed rules from source |
| `--check` | Audit only; exit 1 on drift, 2 on tampering, 0 clean |
| `--diff ID` | Unified diff between installed body and source body |
| `--dry-run` | Print what would change without writing |
| `--force` | Bypass tamper checks and rebuild the zone |
| `--replace-stopgap` | Strip known stopgap inline rule paragraphs before injecting |
| `--target PATH` | Override CLAUDE.md target (default: `~/.claude/CLAUDE.md`) |
| `--rules-dir PATH` | Override source rules directory (default: `context-rules/`) |

Exit codes: `0` clean, `1` drift detected, `2` tampering detected, `64` usage error.

## runner/run.py

End-to-end runner orchestrator. Sequences all 6 stages, passes artifact paths via UUID convention, stops on any stage failure.

```bash
python -m runner.run runner/intake/<uuid>.json
python -m runner.run runner/intake/<uuid>.json --target-repo /path/to/other/repo
```

| Argument / Flag | Required | Description |
|-----------------|----------|-------------|
| `intake_path` | yes | Path to intake JSON file (`runner/intake/<uuid>.json`) |
| `--target-repo PATH` | no | Run against a non-apiary git repo. Precedence: CLI flag > intake `target_repo` field > config `runner.target_repo` > apiary fallback. Path must exist and contain a `.git` entry. |

Stages run in order: validate_intake → auto_refine → auto_plan → executor → auto_harden → approval. Each stage's input path is derived from the UUID. Prints per-stage status and elapsed time. Exit 0 if all stages pass; exit 1 on first failure.

Stage timeout is configurable via `runner/config.json` under `orchestrator.stage_timeout` (default: 3600s).

### Multi-repo (`--target-repo`)

All runner artifacts (specs/plans/executions/hardens/reports) always live under apiary's `runner/<dir>/<uuid>.json`, regardless of target. Only the executor's code-change diff lands in the target repo, on a `runner/<slug>-<uuid>` branch off its `master`. A single apiary checkout therefore holds the centralized run history across every target repo it has run against; each `run_history.jsonl` entry carries a `target_repo` field to disambiguate.

## runner/create_intake.py

Create an intake file for the autonomous runner. Generates a UUID-keyed JSON at `runner/intake/<uuid>.json`.

```bash
python -m runner.create_intake --title "Add caching" --problem "Repeated DB queries" --description "Add Redis cache layer" --scope "api/cache.py"
python -m runner.create_intake --from-todo T-2026-42
```

| Flag | Required | Description |
|------|----------|-------------|
| `--title TEXT` | yes* | Short title for the task |
| `--problem TEXT` | yes* | Problem statement (min 20 chars) |
| `--description TEXT` | yes* | Detailed description (min 20 chars) |
| `--scope TEXT` | yes* | What's in scope for this runner run |
| `--context TEXT` | no | Additional context (optional) |
| `--explore-hints CSV` | no | Comma-separated repo-relative paths the refiner should start with (refiner can still branch out) |
| `--from-todo ID` | no | Scribe TODO ID to seed from (replaces manual fields) |

\* Required unless `--from-todo` is used.

## runner/refine_to_intake.py

Bridge a refiner handoff scribe note into a runner intake (or backlog draft) file. The `/refine` skill saves approved handoffs as scribe notes of type `context` with a fixed section layout (`## Goal`, `## Shape`, `## Behavior`, `## Boundaries`, `## Acceptance criteria`); this script parses those sections and maps them onto the intake schema.

```bash
python -m runner.refine_to_intake --note C-2026-5 --title "Add caching"
python -m runner.refine_to_intake --note C-2026-5 --title "Add caching" --backlog
python -m runner.refine_to_intake --note C-2026-5 --title "Add caching" --explore-hints "api/cache.py,api/db.py"
```

| Flag | Required | Description |
|------|----------|-------------|
| `--note ID` | yes | Scribe note ID containing the refiner handoff |
| `--title TEXT` | yes | Short title (refiner handoffs have no title field) |
| `--backlog` | no | Write to `runner/backlog/<slug>.json` instead of `runner/intake/<uuid>.json` |
| `--explore-hints CSV` | no | Comma-separated repo-relative paths for the auto-refiner |

Mapping: `Goal > **Problem:**` → `problem`; `Shape` + `Behavior` → `description`; `Boundaries` → `scope`; `Acceptance criteria` → `context`. On intake mode the file is validated via `validate_intake` and deleted on failure. The written record sets `source` to `scribe-note:<id>`.

## runner/validate_intake.py

Validate an intake JSON file. Checks required fields, types, minimum content thresholds, and ISO date format.

```bash
python -m runner.validate_intake runner/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to intake JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## runner/auto_refine.py

Autonomous refiner — Stage 2. Reads a validated intake JSON, launches a Claude Code subprocess to explore the codebase and produce a structured spec.

```bash
python -m runner.auto_refine runner/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `intake` | yes | Path to intake JSON file |

Output: `runner/specs/<uuid>.json`. Model and retries configurable via `runner/config.json` under `refine`.

## runner/validate_spec.py

Validate a spec JSON file against the 8 handoff validation rules.

```bash
python -m runner.validate_spec runner/specs/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to spec JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## runner/auto_plan.py

Autonomous planner — Stage 3. Reads a validated spec JSON, launches a Claude Code subprocess to produce a step-by-step implementation plan.

```bash
python -m runner.auto_plan runner/specs/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `spec` | yes | Path to spec JSON file |

Output: `runner/plans/<uuid>.json`. Model and retries configurable via `runner/config.json` under `plan`.

## runner/validate_plan.py

Validate a plan JSON file for the autonomous runner.

```bash
python -m runner.validate_plan runner/plans/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to plan JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## runner/executor.py

Executor — Stage 4. Reads a validated plan JSON, creates a feature branch (`runner/<uuid>`), and executes each step via Claude Code subprocess.

```bash
python -m runner.executor runner/plans/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `plan` | yes | Path to plan JSON file |

Output: `runner/executions/<uuid>.json`. Creates branch `runner/<uuid>`. Model and retries configurable via `runner/config.json` under `executor`.

**Edge case:** Fails if branch `runner/<uuid>` already exists (not idempotent for re-runs).

## runner/auto_harden.py

Autonomous hardener — Stage 5. Runs attack-defend rounds against the executor's code changes using the existing `harden/` infrastructure.

```bash
python -m runner.auto_harden runner/executions/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `execution_log` | yes | Path to execution log JSON |

Output: `runner/hardens/<uuid>.json`. Rounds, models, and timeout configurable via `runner/config.json` under `harden` (default: 1 round).

Verdicts written to the artifact:
- `all_resolved` — every finding fixed/refactored (or no findings).
- `has_unresolved` — some findings deferred or unresolved; human review needed.
- `defender_failed` — attacker produced findings but defender returned no responses; run is structurally broken. Reviewer sees this in `queue.py`'s HARDEN column.

## runner/approval.py

Approval — Stage 6. Reads the harden verdict and either auto-merges (all resolved), flags for review (unresolved findings), or halts on `defender_failed` without merging or writing a note. Includes a deferral review sub-step that uses Claude to evaluate deferred findings on the `has_unresolved` path.

```bash
python -m runner.approval runner/hardens/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `harden_result` | yes | Path to harden result JSON |

Output: `runner/reports/<uuid>.json`. Path taken: `auto-merged`, `pending-review`, `defender-failed`, or a merge/push error. Exits non-zero on `defender_failed` so `overnight.jsonl` records the failure.

## runner/draft_ticket.py

Create a backlog draft ticket. Writes a JSON to `runner/backlog/<slug>.json`. Slug is derived from the title.

```bash
python -m runner.draft_ticket --title "..." --problem "..." --description "..." --scope "..."
python -m runner.draft_ticket --from-todo T-2026-42 --title "..." --problem "..." --scope "..."
```

| Flag | Required | Description |
|------|----------|-------------|
| `--title TEXT` | yes | Short title (used to generate the slug filename) |
| `--problem TEXT` | yes | Problem statement |
| `--description TEXT` | yes* | Detailed description |
| `--scope TEXT` | yes | Scope of work |
| `--context TEXT` | no | Additional context (optional) |
| `--from-todo ID` | no | Scribe note ID — only auto-fills `--description` from the note content; `--title`, `--problem`, and `--scope` are still required |

\* Required unless `--from-todo` is provided (which fills it from the note).

**Gotcha:** `--from-todo` is *not* a one-stop shortcut — it only seeds `--description`. You must still pass `--title`, `--problem`, and `--scope` explicitly.

## runner/promote.py

Promote a backlog draft to a runner intake file. Validates against the intake schema, assigns a UUID, copies to `runner/intake/<uuid>.json`, and removes the backlog file.

```bash
python -m runner.promote <slug>
```

| Argument | Required | Description |
|----------|----------|-------------|
| `slug` | yes | Backlog ticket slug — the filename **without** directory or `.json` extension |

**Gotcha:** Pass the slug only (e.g. `my-feature`), not a path (e.g. `runner/backlog/my-feature.json`). Path separators are rejected to prevent traversal. The script always looks in `runner/backlog/<slug>.json`.

## runner/mark_done.py

Mark a backlog ticket as done **without** running it through the runner. For tickets small enough to fix by hand. Deletes `runner/backlog/<slug>.json`.

```bash
python -m runner.mark_done <slug> [--note "explanation"]
```

| Argument / Flag | Required | Description |
|-----------------|----------|-------------|
| `slug` | yes | Backlog ticket slug — the filename **without** directory or `.json` extension |
| `--note TEXT` | no | Optional note describing the manual completion (currently informational only) |

The presence of the backlog file is itself the safety check — `promote.py` removes the backlog file when a ticket enters intake, so a backlog file that still exists is guaranteed not to be in flight. Use `promote.py` first if you actually want the ticket to run through the runner.

## runner/cost_emit.py

Shared helper used by every stage's `run_claude` wrapper. Library module — not a CLI tool. Parses a `claude -p --output-format json` envelope and emits a `<usage>` XML block to stderr that the orchestrator scrapes for cost tracking.

```python
from cost_emit import emit_usage_xml
emit_usage_xml(claude_subprocess_stdout)  # writes <usage>...</usage> to stderr
```

Silent on any failure — cost logging never breaks a stage. Sums all numeric fields under the envelope's `usage` key (input + output + cache_*) into a single `total_tokens` value.

## runner/cron_health.py

Check or repair the host OS scheduler against apiary's canonical scheduled-entry registry (`cron_registry/<hostname>.json` at the apiary repo root). Each machine maintains its own file — named after `platform.node()` — so multi-machine setups don't fight over a single shared registry. Detects drift when the repo moves, files rename, or the registered command points at a path that no longer exists.

```bash
python ~/.claude/apiary_launch.py runner/cron_health.py check
python ~/.claude/apiary_launch.py runner/cron_health.py repair [--apply]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `check` | Read-only inspection; prints a status table, exit 0 when everything matches the registry |
| `repair` | Dry-run by default (prints intended changes); pass `--apply` to execute delete + recreate against the scheduler |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All entries match the registry (or `repair` dry-run succeeded) |
| `1` | Drift detected, or one or more operations failed during `repair --apply` |
| `2` | Config or platform error (registry missing, malformed JSON, unsupported OS, scheduler binary not on PATH) |

### Platform support

Windows Task Scheduler only in this release — backed by `schtasks`. The scheduler-backend protocol (`runner/schedulers/base.py`) keeps launchd (macOS) and crontab (Linux) cheap to add when there's a real user for them. Running on any other platform exits with code 2 and a "not supported" message.

`scripts/bootstrap.py` invokes `check` at the tail of its run, prints the status table, and never propagates drift into its own exit code — the check is informational only.

## runner/config_loader.py

Shared config loader. Library module — not a CLI tool. Used by runner stages to read `runner/config.json`.

```python
from config_loader import get as cfg
timeout = cfg("orchestrator", "stage_timeout", 3600)
```

Falls back to defaults if `runner/config.json` is missing.

## gui/app.py

Native Windows desktop wrapper — spawns Claude Code as a hidden pty subprocess and presents a clean filtered chat view, multi-tab cwd switching, and a global scribe sidebar. Requires the `gui` poetry group (pywebview, pywinpty, watchdog) — explicit deviation from the stdlib-only rule (decision `D-2026-47`).

Run from source:

```bash
poetry run python -m gui.app
```

### Environment variables

| Var | Description |
|-----|-------------|
| `APIARY_GUI_PROFILE` | Re-roots state, mutex name, and window title — see `gui/paths.py`. Set to e.g. `dev` to run a second instance alongside the default one. State goes to `~/.claude/apiary_gui_<profile>/`; window title becomes `apiary [<profile>]`. |
| `APIARY_GUI_CAPTURE_LABEL` | Enables raw pty-output capture for the session (writes to `~/.claude/apiary_gui/captures/<ts>-<label>.bin`). Used by `gui/capture_session.py`. |
| `APIARY_PERMISSION_MCP` | One-shot override for the `permission_mcp` flag in `launch.json`. `"1"` forces the structured MCP permission-prompt path on; any other value (including `"0"`) forces it off. When unset, the GUI falls back to the `launch.json` value (defaults to off). Enabling routes prompts through `gui/permission_mcp.py` + loopback HTTP bridge instead of the TUI-banner scraper; the GUI boots the bridge and appends `--mcp-config`/`--permission-prompt-tool` to the claude argv. See scribe `C-2026-36`. |
| `APIARY_PERMISSION_MCP_URL` | Exported automatically by the GUI to the bridge's loopback URL so the spawned MCP subprocess can POST decisions back. Do not set by hand. |
| `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` | Pass-through to WebView2; the app appends `--disable-cache` flags so frontend edits aren't masked by the static-asset cache. |

## gui/capture_session.py

Convenience wrapper around `gui.app` that sets `APIARY_GUI_CAPTURE_LABEL` for one launch, used to record raw pty bytes for new prompt-detector fixtures.

```bash
poetry run python -m gui.capture_session --label tool_permission   # capture one session
poetry run python -m gui.capture_session list                       # list existing captures
```

Captures are binary (pre-decode) so ANSI escapes survive intact.

## gui/packaging/build.py

PyInstaller one-folder build for the GUI. Cleans `build/` and `dist/apiary-gui/` first, then invokes `pyinstaller gui/packaging/apiary_gui.spec`. Outputs `dist/apiary-gui/apiary-gui.exe` plus its `_internal/` sibling.

```bash
poetry run pip install "pyinstaller>=6.0,<7.0"   # one-time, build-only
poetry run python gui/packaging/build.py
```

The spec bundles `gui/web/` under `_internal/gui/web/`, embeds the PerMonitorV2 HiDPI manifest, and embeds the three-hex `apiary_gui.ico` taskbar icon.

## gui/packaging/make_icon.py

Regenerates `gui/packaging/apiary_gui.ico` (multi-resolution 16/32/48/128/256) — three flat-top hexagons in a triangular cluster, accent-blue on transparent. Pillow-based; build-time only.

```bash
poetry run pip install "Pillow>=10,<12"          # one-time, build-only
poetry run python gui/packaging/make_icon.py
```

## core/apiary_bootstrap.py

Apply an apiary profile to a target repo's `.claude/settings.json`. Source at `core/apiary_bootstrap.py`; installed to `~/.claude/apiary_bootstrap.py` by `setup.py --global`, which is the canonical invocation path.

```bash
python ~/.claude/apiary_bootstrap.py --profile <name> [--target PATH] [--force] [--apiary-repo PATH]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--profile NAME` | yes | Profile name under `<apiary-repo>/profiles/<name>.jsonc` |
| `--target PATH` | no | Target repo root (default: cwd) |
| `--force` | no | Skip the re-run drift prompt and the first-run wipe prompt; apply changes non-interactively. Warnings still print. |
| `--apiary-repo PATH` | no | Override apiary repo location (default: via `~/.claude/apiary.json` pointer) |

Walks the profile's `extends` chain, deep-merges parents left-to-right then the child on top (`{"$replace": value}` escape hatch replaces instead of merges), and writes the resolved apiary-owned top-level keys into `.claude/settings.json`, preserving non-apiary keys verbatim. State lands at `.apiary/bootstrap_state.json` (schema version, profile chain, per-profile content hashes, last bootstrap timestamp).

First-run safety: if the target has an existing `settings.json` with content inside apiary-owned keys that the profile doesn't set, a warning prints listing the about-to-be-wiped entries and recommends moving them to `.claude/settings.local.json` (Claude Code merges that file natively). The tool prompts before applying; `--force` skips the prompt but still prints the warning.

Re-runs detect drift against the stored state: if the new merge would change the current `settings.json`, a per-key diff prints and the tool prompts before applying. `--force` skips the prompt. Non-TTY stdin without `--force` is a hard error in both the first-run-wipe and re-run-drift paths.

Exit codes:
- `0` — success, or re-run no-op
- `1` — aborted by user, or non-TTY re-run without `--force`
- `2` — profile not found, extends cycle, unsupported `$schema_version`, or JSONC parse error

See [Bootstrapping a repo](../guides/bootstrapping-a-repo.md) for profile authoring and the full workflow.

## core/targets.py

Inspect and verify the apiary target registry at `<apiary>/.repos/registry.json` (built by the resolver in `core/utils/state.py`). Every target whose `.apiary/` state has been created on this machine is indexed there; this tool reports the index and flags entries whose `real_path` no longer exists on disk.

```bash
python ~/.claude/apiary_launch.py core/targets.py list
python ~/.claude/apiary_launch.py core/targets.py verify
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `list` | Tabular print of every registered target (id, name, status, last_used, real_path). Prints `No registered targets.` on a fresh checkout. |
| `verify` | Walk every target, set `verified_ok` based on whether `real_path` is still a directory, stamp `last_verified`, and report missing paths. Always exits 0 unless registry IO fails — the verification *result* is in stdout, the *command* itself succeeded. |

`verify` updates the registry under a file lock; missing entries are reported but not pruned (pruning is a separate, future operation — review-then-decide).

Spec: scribe note `C-2026-46`.

## Test scripts

All tests use `unittest` and are run directly:

```bash
python budgeter/test_hooks.py
python scribe/test_notes.py
python harden/test_validators.py
python harden/test_assign_ids.py
python -m runner.test_orchestrator
```
