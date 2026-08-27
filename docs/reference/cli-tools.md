---
type: reference
title: CLI Tools
scope: project
description: All Python CLI entry points with subcommands, flags, and usage examples
framework_version: "1.0"
last_verified: "2026-08-26"
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
| `tidy` | `notes.py tidy` | Run the auto-archive retention sweep now. `add` and session startup run the same sweep; `list` never does |
| `mark-reviewed` | `notes.py mark-reviewed` | Stamp `<state-dir>/scribe/learnings/last_review` — the marker the startup banner's review nudge reads |
| `learn` | `notes.py learn (--content "<text>" \| --content-file PATH)` | Add a learning |
| `learnings` | `notes.py learnings` | List all learnings |
| `unlearn` | `notes.py unlearn <ID>` | Remove a learning (e.g. `L-2026-3`) |
| `drop` | `notes.py drop <ID>` | Close a note without marking it done (status → dropped) |
| `unarchive` | `notes.py unarchive <ID>` | Move a note back from its year's archive to active |
| `show` | `notes.py show <ID>` | Alias of `get` |
| `template` | `notes.py template show <type>` | Inspect the per-type templates that gate `add` (sub-actions: `show`, `path`, `list`) |
| `supersede` | `notes.py supersede <ID> --content "<text>"` | Archive a learning and write a replacement |
| `archive-learning` | `notes.py archive-learning <ID>` | Archive a learning by ID (e.g. `L-2026-5`) |
| `repair` | `notes.py repair [--dry-run]` | Repair index/data inconsistencies |
| `backfill-brief` | `notes.py backfill-brief [--dry-run] [--force]` | Populate `brief_summary` on entries that lack one |

> **Note IDs** use TYPE-YEAR-seq format (e.g. `T-2026-1`, `L-2026-3`) — the only accepted form. See `scribe/CLAUDE.md` for the full prefix table.

### Common flags

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--project PROJECT` | all | Project key override (default: derived from cwd) |
| `--type TYPE` | add, list | Note type: `todo`, `handoff`, `decision`, `wishlist`, `reference`, `blocker`, `context`, `general`. Learnings are a separate store — use the `learnings` subcommand, not `--type learning` |
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
| `--content-file PATH` | add, learn | Read content from a UTF-8 file instead of `--content`. Mutually exclusive with `--content`; one of the two is required. Use it for any body that is long or awkward on argv — backticks and `/`-prefixed tokens trigger shell substitution, and Windows caps a command line at 32,767 chars, so multi-kilobyte content (an incubator spec, a `/wrapup` handoff) must go through a file |
| `--summary TEXT` | add | One-line abstract shown in lists and startup. Required for `--type handoff` |
| `--brief-summary TEXT` | add, update, learn | One-sentence GUI-sidebar headline; auto-derived if omitted |
| `--unique-tag TAG` | add | Add the tag only if no active note already carries it; otherwise skip the add (exit 0) |
| `--add-tag TAG` | update | Add a tag (repeatable, order-preserving, idempotent) |
| `--remove-tag TAG` | update | Remove a tag (repeatable; applied before `--add-tag`) |
| `--session SESSION` | list | Filter by session ID |
| `--index` | learnings | Compact tag-grouped output for startup injection |
| `--tag TAG` | learnings | Filter learnings by tag (substring, case-insensitive) |
| `--tags LIST` | add, learn, supersede | Comma-separated tag list. Stored verbatim on `add`; on `learn`/`supersede`, omit to infer via `claude -p` |
| `--area GLOB` | learn, supersede, learnings | Area glob — repeatable on `learn`/`supersede`; exact-match filter on `learnings` |
| `--supersedes ID` | learn | ID of a prior learning this one replaces (e.g. `L-2026-5`) |
| `--dry-run` | repair, backfill-brief | Report what would change without writing |
| `--force` | add, backfill-brief | On `add`, bypass the template gate's required-section check (logs to stderr what was missing); on `backfill-brief`, re-derive `brief_summary` even for entries that already have one |

### Note templates

`apiary install` seeds `<state-dir>/scribe/templates/<type>.md` from the bundled defaults in `scribe/default_templates/` — one per note type, and an existing file is never overwritten. A template whose frontmatter declares `required: [...]` makes `add` reject content that omits any of those sections (heading or `**Bold:**` label, case-insensitive); a template with no `required:` key is guidance only and never blocks. Defaults that enforce: `handoff` (What was done / Key decisions / What's pending / Where it stopped, matching `/wrapup`), `decision` (Context / Decision / Why / Consequences), `blocker` (Blocked on / Tried / Unblock when). The check is forward-only — it runs on `add`, never on existing notes — and `--force` bypasses it.

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

Copies `index.jsonl` files only — the `.md` bodies and the per-year `next_seq` counters are not backed up. Not scheduled anywhere today; exits 0 even when no state dir exists (the first `/apiary-context` call will create one).

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

Consistency checks for the per-repo install model. Every check is read-only;
`--fix` opts into the named check's writer, where one exists. Reached in normal
use as `apiary doctor` (see the `apiary` section) — the module form below is for
running it out of a checkout that has no console script installed.

```bash
poetry run apiary doctor [subcommand] [--fix] [--apiary-repo PATH]
python core/doctor.py [subcommand] [--fix] [--apiary-repo PATH]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| (none) | Run all checks, print a summary |
| `pointers` | Verify main-apiary's self-pointer matches its actual location |
| `registry` | Walk every registered repo: path exists; uid/version fields present |
| `versions` | Compare each repo's pinned version against `<apiary>/VERSION` |
| `stale` | Registered repos whose installed slash-command files differ from current main-apiary source (skill drift) |
| `orphans` | Folders under `.repos/<slug>/` whose UID has no registry entry |
| `duplicates` | Registry entries sharing a `real_path` |
| `unreachable` | Registry entries whose `real_path` does not exist on disk |

### Flags

| Flag | Description |
|------|-------------|
| `--fix` | Apply the named check's safe fix. Supported for `pointers` (cascade every bootstrapped repo's pointer to the current main-apiary path). Requires a subcommand. |
| `--apiary-repo PATH` | Path to main-apiary checkout (default: resolved via launcher / pointer) |

### Exit code

- `0` — all checks pass (notes are informational and do not fail the run).
- `1` — any check reported an issue, or a `--fix` run hit an error.
- `2` — `--fix` without a subcommand, or on a check that has no fix.

## core/flags.py

Feature-flag toggles for apiary tools. Each flag is a sentinel file at
`<repo>/.claude/apiary/flags/<name>-enabled` — presence means enabled, absence
means disabled. The `/budgeter` slash command drives this CLI; hooks read the
same files in-process via `flags.is_enabled(name)`.

```bash
python core/flags.py toggle budgeter-log
python core/flags.py status budgeter-session-warn
```

The repo is resolved from `$CLAUDE_PROJECT_DIR`, then `$APIARY_TARGET_REPO`,
then the git root containing the cwd.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `toggle` | `flags.py toggle NAME` | Flip the flag, print its new state |
| `enable` | `flags.py enable NAME` | Create the flag file, print `ON` (idempotent) |
| `disable` | `flags.py disable NAME` | Remove the flag file, print `OFF` (idempotent) |
| `status` | `flags.py status NAME` | Print the current state without changing it |

### Arguments and flags

| Argument / Flag | Applies to | Required | Description |
|-----------------|-----------|----------|-------------|
| `NAME` | all | yes | Flag name — `budgeter-log`, `budgeter-session-warn`, `auto-startup`. Letters, digits, `.`, `_`, `-` only |

### Output and exit codes

Prints exactly `ON` or `OFF` on stdout — for `toggle`, that is the state *after*
the flip.

- `0` — the verb ran; stdout holds the resulting state.
- `1` — no bootstrapped repo is in scope, or the flag name is malformed (reason on stderr).
- `2` — argparse usage error (unknown verb, missing name).

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
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/synthesize.py
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/synthesize.py --dry-run
python -m compass.synthesize --cron        # cron-driven; no-ops if personality.md is < 7 days old
```

| Flag | Required | Description |
|------|----------|-------------|
| `--dry-run` | no | Print the synthesis prompt instead of calling claude |
| `--model MODEL` | no | Override the claude CLI's default model |
| `--cron` | no | Self-throttle to a 7-day cadence (no-op if `personality.md` was rewritten in the last week) |
| `--max-sessions N` | no | Synthesize from at most the N most recent sessions by `captured_at` (default 50, matching the archive threshold; `0` disables the cap) |

Exit codes: `0` wrote `personality.md`; `1` no active observations; `2` claude subprocess failed (previous file untouched).

## compass/backfill.py

Extract observations from historical session transcripts via headless claude. Selectors are combinable and intersected.

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/backfill.py --last 5
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/backfill.py --session-ids 1089da5c,8123e697
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/backfill.py --since 2026-04-10 --last 5
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
| `verify` | `cli.py verify --path <abs-path>` | Check that a path is a complete, working spawn (see below) |

### Flags

| Flag | Required | Description |
|------|----------|-------------|
| `--path` | yes | Absolute target directory; must not exist; parent must exist; must not be inside an existing git repo |
| `--spec-note-id` | yes | ID of the `/refine` context note in apiary scribe (e.g. `C-2026-43`) |
| `--author` | no | Author string for `pyproject.toml`; defaults to git config `user.name <user.email>` |
| `--session-id` | no | Optional session ID stamped on the migrated spec note |

Exit codes: `0` success; `2` validation error (bad path); `3` spec note not found; `4` spawn failure (rolled back automatically); `5` partial success — repo created but spec migration failed (recover manually).

Templates that get written into the new repo live under `incubator/templates/` (`gitignore.tmpl`, `pyproject.toml.tmpl`, `CLAUDE.md.tmpl`).

### verify

Check that a target path is a complete, working spawn. Prints a pass/miss table and exits non-zero if anything is missing.

```bash
python .claude/apiary/launch.py incubator/cli.py verify --path /abs/path/to/repo
```

| Flag | Description |
|------|-------------|
| `--path PATH` | Target directory to verify (required) |

Checks: `.git/`, `.claude/apiary/launch.py`, `pyproject.toml`, `CLAUDE.md`, `.gitignore`, a registry entry in main-apiary whose `real_path` is this repo, and the secret-scan pre-commit hook.

Exit codes: `0` all checks pass; `2` no such directory; `6` one or more checks failed.

`spawn` runs the same checks before reporting success, so it also exits `6` when a freshly-created repo fails them.

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
echo '<json>' | python harden/validate_and_assign.py findings [--lens NAME] [--check-files] [--deep] [--sanitize]
python harden/validate_and_assign.py findings --file findings.json --lens security --sanitize [--check-files] [--deep]
python harden/validate_and_assign.py response --file response.json --expected-ids ATK-001,ATK-002 [--check-files]
python harden/validate_and_assign.py consolidation --file consolidation.json --source-ids ATK-SEC-001,ATK-COR-002 [--check-files]
python harden/validate_and_assign.py consolidation --degrade --file merged_findings.json
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `findings` | Validate and assign IDs to Attacker findings. Legacy mode → `ATK-NNN`; lens mode (`--lens`) → `ATK-<CODE>-NNN` |
| `response` | Validate and assign DEF-IDs to Defender response (prefix-agnostic `--expected-ids`) |
| `consolidation` | Validate Consolidator/referee output and assign `CON-NNN` to accepted findings (multi-lens path) |

### Flags

| Flag | Applies to | Required | Description |
|------|-----------|----------|-------------|
| `--file PATH` | all | no | Read JSON from file instead of stdin |
| `--check-files` | all | no | Verify referenced files exist (code mode) |
| `--deep` | findings | no | Require Given/When/Then scenarios |
| `--sanitize` | findings | no | Auto-fix common issues (strip unknown fields, map categories; in lens mode inject `lens`) |
| `--lens NAME` | findings | no | Per-lens mode: validate against the 7-lens vocab and assign `ATK-<CODE>-NNN` |
| `--expected-ids IDS` | response | yes | Comma-separated finding IDs (`ATK-NNN`, `ATK-<CODE>-NNN`, or `CON-NNN`) that must be addressed |
| `--source-ids IDS` | consolidation | no | Comma-separated `ATK-<CODE>-NNN` ids dispatched; enables exact coverage checking |
| `--degrade` | consolidation | no | Deterministic fallback: dedup raw merged findings by location, assign `CON-NNN`, no adjudication |

Exit 0 + validated JSON with IDs on success. Exit 1 + error details on failure.

## harden/lenses.py

Single source of truth for the 7-lens taxonomy used by the multi-lens harden flow: lens names, their 3-letter ID codes (`ATK-<CODE>-NNN`), one-line briefs, and the seam rules. Read by the orchestrator to build per-lens attacker prompts.

```bash
python harden/lenses.py list    # canonical lens names, one per line
python harden/lenses.py codes   # name=CODE pairs (correctness=COR, security=SEC, ...)
python harden/lenses.py json    # full taxonomy: lenses, briefs, seam_rules
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `list` | Print the canonical lens names, one per line |
| `codes` | Print `name=CODE` pairs, one per line |
| `json` | Print the full taxonomy (names, codes, briefs, seams) as JSON |

The seven lenses: `correctness` (COR), `security` (SEC), `robustness` (ROB), `resilience` (RES), `complexity` (CPX), `architecture` (ARC), `testing` (TST).

## harden/validate_consolidation.py

Validate Consolidator (referee) output for the multi-lens path: `accepted`/`rejected` arrays, severity enum, that every dispatched source finding is accounted for exactly once (with `--source-ids`), dedup integrity, and optional file existence. Also exposes the `--degrade` fallback that merges raw per-lens findings by location.

```bash
python harden/validate_consolidation.py --file consolidation.json --source-ids ATK-SEC-001,ATK-COR-002 [--check-files]
python harden/validate_consolidation.py --degrade --file merged_findings.json
```

| Flag | Required | Description |
|------|----------|-------------|
| `--source-ids IDS` | no | Comma-separated `ATK-<CODE>-NNN` ids dispatched to the consolidator; enables coverage checking |
| `--check-files` | no | Verify accepted-finding files exist (code mode) |
| `--degrade` | no | Fallback: dedup raw merged findings by location instead of validating |
| `--file PATH` | no | Read JSON from file instead of stdin |

Exit 0 + validated JSON on success. Exit 1 + error details on failure.

## harden/assign_ids.py

Assign deterministic sequential IDs to harden agent output. Reads JSON array from stdin or file.

```bash
echo '<json_array>' | python harden/assign_ids.py --prefix ATK
python harden/assign_ids.py --prefix ATK-SEC --file findings.json
```

| Flag | Required | Description |
|------|----------|-------------|
| `--prefix PREFIX` | yes | ID prefix: `ATK` (legacy findings), `ATK-<CODE>` (per-lens findings), `CON` (consolidated), or `DEF` (responses) |
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
| `--lens NAME` | no | Validate as lens-mode findings for the given lens (replaces the legacy category field) |

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

Track harden round counts per session. Also used by `/refine`, which scopes its own counter with a `refine-` prefix on `--session-id`.

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

## scripts/preflight.py

Pre-install environment check, run by `scripts/install.ps1` / `scripts/install.sh` (and usable standalone) before `poetry install`. Reports every missing or fragile prerequisite at once — Python version, git, install-path sanity (spaces / apostrophes / non-ASCII), and the `claude` CLI — instead of surfacing them one cryptic failure at a time. With `--gui` it also checks the GUI's Python pin (3.11/3.12, since pythonnet has no 3.13+ wheel) and the Edge WebView2 runtime. Stdlib only and imports nothing from apiary, so it runs on a bare clone whose dependencies are not installed yet.

```bash
python scripts/preflight.py            # base install checks
python scripts/preflight.py --gui      # also check desktop GUI prerequisites
```

| Flag | Description |
|------|-------------|
| `--gui` | Also check desktop GUI prerequisites (pythonnet Python pin, WebView2 runtime) |

Exit codes: `0` no hard blockers (warnings allowed; install can proceed), `1` a blocker must be fixed first.

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
| `--resume-from STAGE` | no | Resume from a specific stage (skip earlier stages) |
| `--detached` | no | Detached (cron) mode: pick from backlog, branch, commit, log |
| `--token-cap N` | no | Per-run token cap (detached mode); default from config `detached.token_cap` |
| `--max-unreviewed N` | no | Max unmerged runner branches before skipping (detached mode) |
| `--cleanup UUID` | no | Delete runner branch(es) / worktree for the given uuid |
| `--abort UUID` | no | Abort a crashed run: archive artifacts, remove worktree |
| `--prune-failed` | no | Prune failed/abandoned runner branches (with `--older-than`) |
| `--older-than DAYS` | no | Age threshold for `--prune-failed` (default 7) |
| `--dry-run` | no | List prune candidates without deleting anything |

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

Approval — Stage 6. Reads the harden verdict and either squash-merges to master **locally** (all resolved — it never pushes; a todo asks the operator to review and push), flags for review (unresolved findings), or halts on `defender_failed` without merging or writing a note. Includes a deferral review sub-step that uses Claude to evaluate deferred findings on the `has_unresolved` path.

```bash
python -m runner.approval runner/hardens/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `harden_result` | yes | Path to harden result JSON |

Output: `runner/reports/<uuid>.json`. Path taken: `merged-locally`, `pending-review`, `defender-failed`, or a merge error. Exits non-zero on `defender_failed` so `run_history.jsonl` records the failure.

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
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" runner/cron_health.py check
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" runner/cron_health.py repair [--apply]
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
| `APIARY_GUI_PROFILE` | Re-roots state, mutex name, and window title — see `gui/paths.py`. Set to e.g. `dev` to run a second instance alongside the default one. State goes to `<main-apiary>/.apiary/gui/apiary_gui_<profile>/`; window title becomes `apiary [<profile>]`. |
| `APIARY_GUI_CAPTURE_LABEL` | Enables raw pty-output capture for the session (writes to `<main-apiary>/.apiary/gui/apiary_gui/captures/<ts>-<label>.bin`). Used by `gui/capture_session.py`. |
| `APIARY_PERMISSION_MCP` | One-shot override for the `permission_mcp` flag in `launch.json`. `"1"` forces the structured MCP permission-prompt path on; any other value (including `"0"`) forces it off. When unset, the GUI falls back to the `launch.json` value (defaults to off). Enabling routes prompts through `gui/permission_mcp.py` + loopback HTTP bridge instead of the TUI-banner scraper; the GUI boots the bridge and appends `--mcp-config`/`--permission-prompt-tool` to the claude argv. See scribe `C-2026-36`. |
| `APIARY_PERMISSION_MCP_URL` | Exported automatically by the GUI to the bridge's loopback URL so the spawned MCP subprocess can POST decisions back. Do not set by hand. When it is unset the MCP server **denies** every request (fail closed) — a bridge that failed to boot, or a stale `permission_mcp_config.json` used outside the GUI, never becomes a rubber stamp. The GUI only sets `APIARY_PERMISSION_MCP=1` after the bridge is listening and pins it to `0` if the bind fails. |
| `APIARY_PERMISSION_MCP_ALLOW_ALL` | `"1"` makes the MCP server auto-allow when no bridge URL is set. For headless tests of the MCP plumbing only; the GUI never sets it and it has no effect while a bridge URL is present. |
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

## apiary

The unified CLI registered as a console_script by `pyproject.toml` (run as
`poetry run apiary <subcommand>`). Source at `core/cli.py`; each verb dispatches
to a single-purpose module under `core/`.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `install` | `apiary install --target <repo> [--profile <name>]` | Bootstrap apiary into a target repo (`core/install.py`). Idempotent. |
| `uninstall` | `apiary uninstall --target <repo> [--remove-data]` | Reverse of install (`core/uninstall.py`). |
| `self-bootstrap` | `apiary self-bootstrap` | First-machine setup of main-apiary; equivalent to running `install --target` on main-apiary itself (`core/self_bootstrap.py`). |
| `doctor` | `apiary doctor [check] [--fix]` | Consistency checks (`core/doctor.py`). Checks: `pointers`, `registry`, `versions`, `stale`, `orphans`, `duplicates`, `unreachable` — name one, or omit to run all. See the `core/doctor.py` section for what each reports. |
| `cascade-fix` | `apiary cascade-fix` | Rewrite every bootstrapped repo's `main-apiary-pointer.json` to the current main-apiary path (`core/cascade.py`). |
| `version` | `apiary version` | Print main-apiary's pinned version (the contents of `<main-apiary>/VERSION`). |

### Flags

| Flag | Applies to | Required | Description |
|------|-----------|----------|-------------|
| `--target PATH` | install, uninstall | yes | Target repo. |
| `--profile NAME` | install | no | Profile under `<main-apiary>/profiles/` (default: `base`). |
| `--remove-data` | uninstall | no | Also delete `<main-apiary>/.repos/<name>-<uid>/` (the centralized per-target state). |
| `--fix` | doctor | no | Apply the named check's safe fix. Only `pointers` (cascade the pointer rewrite) has one; `--fix` without a check name, or on any other check, exits `2`. |
| `--apiary-repo PATH` | all | no | Path to main-apiary. Default: resolved via `APIARY_MAIN_REPO`, the running source tree, or `<cwd>/.claude/apiary/main-apiary-pointer.json`. |

`install` walks the profile's `extends` chain, deep-merges parents left-to-right then the child on top (`{"$replace": value}` escape hatch replaces instead of merges), and writes the resolved apiary-owned top-level keys into `<repo>/.claude/settings.json`. It also generates `<repo>/.claude/apiary/{launch.py, main-apiary-pointer.json, self-pointer.json, version.json}`, copies slash commands into `<repo>/.claude/commands/`, writes the apiary-managed zone into `<repo>/CLAUDE.md`, updates `<repo>/.gitignore`, and installs the commit-time secret-scan pre-commit hook (best-effort — a refusal warns instead of failing the install). Centralized state lands at `<main-apiary>/.repos/<name>-<uid>/bootstrap_state.json` (schema v2 — adds the file hashes `apiary doctor stale` compares against to detect slash-command drift).

Exit codes:
- `0` — success; for `doctor`, every check passed.
- `1` — `doctor` reported an issue. `install` / `uninstall` failures (target is not a git repo, unknown profile, `extends` cycle, unsupported `$schema_version`, JSONC parse error) currently surface as an unhandled exception traceback, which also exits `1`.
- `2` — argparse usage error, or `--fix` on a check that has no fix.

See [Bootstrapping a repo](../guides/bootstrapping-a-repo.md) for profile authoring and the full workflow.

## core/hooks/dispatch.py

The hook dispatcher: the **only** hook command in a bootstrapped repo's `.claude/settings.json`. One process per Claude Code event — it reads the payload from stdin once and runs every relevant hook module in-process, in registry order, then prints one merged JSON response. Not something you normally type; documented here because it is the entry point every hook now goes through, and because it is the thing to run by hand when a hook misbehaves.

Takes exactly one positional verb and no flags (a hand-rolled `sys.argv` check, not argparse — this is the hottest path in the toolkit and every import costs milliseconds on every tool call, so it is listed in `check_cli_claims.SKIP_HEADERS`).

```bash
# what settings.json runs
python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" core/hooks/dispatch.py pre

# reproduce a hook chain by hand with a synthetic payload
echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"session_id":"<uuid>","cwd":"<repo>","transcript_path":""}' \
  | python core/hooks/dispatch.py pre
```

| Verb | Event | Chain |
|------|-------|-------|
| `pre` | PreToolUse | drift check, session inject, learnings, research nudge, both push gates, budgeter, standards reminder |
| `post` | PostToolUse | error-rule reminder, budgeter agent-cost logger |
| `stop` | Stop | budgeter stop, transcript saver |
| `prompt` | UserPromptSubmit | startup context injector |
| `session-start` | SessionStart | (none registered yet) |

Exit codes: `0` no objection (the merged `additionalContext`, or `{}`); `2` a gate blocked the call, with the `deny` JSON on stdout and the reason on stderr; `2` also for a bad verb. Never exits nonzero because a hook failed — per-hook exceptions are logged to `<repo>/.claude/apiary/hooks.log` (rotated at 1 MiB) and the chain continues.

The hook contract (`run(payload) -> HookResult | None`), the registry, the matcher semantics and the log format are documented in [hooks.md](hooks.md).

## docs/check_cli_claims.py

Reconcile the CLI claims in `cli-tools.md` against each tool's real argparse — reports drift when a documented subcommand/flag no longer exists, or a real one is undocumented. Sibling to `docs/check.py`; report-only, never rewrites the doc. Shells out to each tool's `--help`. Mark intentional omissions with an inline `<!-- cli-claims: ignore: --some-flag, somesubcmd -->` anywhere in a tool's section.

A section is reconciled when its `##` header is a repo-relative `.py` path, or a console-script name listed in `CONSOLE_SCRIPTS` (the header is the command, so it is mapped to the module behind the entry point — `apiary` → `core/cli.py`). Sections in `SKIP_HEADERS` are libraries, redirect stubs, GUI-dependency scripts, hand-parsed entry points (`core/hooks/dispatch.py`) and prose categories, and are dropped silently; anything else that is not reconcilable is reported as `SKIP` rather than passing quietly. Run by `docs/hooks/pre-commit` on every commit and by `core/hooks/pre_push_doc_conformer.py` on every push — the two gates run the same check, so drift is normally fixed at commit time.

```bash
python docs/check_cli_claims.py
python docs/check_cli_claims.py --only scribe/notes.py
```

| Flag | Description |
|------|-------------|
| `--only HEADER` | Check a single tool section by its `## ` header (e.g. `scribe/notes.py`) |

Exit codes: `0` no drift; `1` drift found; `2` `cli-tools.md` not found.

## scripts/secret_scan.py

Commit-time secret scanner. Stdlib only — git hooks resolve `py -3`/`python3`/`python`, not the Poetry virtualenv, so nothing outside the standard library is importable here, and an external binary like `gitleaks` would break the portability contract. Reads the **staged** diff (added lines only), so it checks exactly what a commit would introduce, and reports file, line, and which pattern matched — with the credential itself redacted, never reprinted. Also blocks filenames that hold credentials by convention (`.env`, `id_rsa`, `*.pem`, `*.key`, `.netrc`, `.git-credentials`, `kubeconfig`, `service-account*.json`, ...) even when `git add -f` bypasses `.gitignore`. Rules live in `core/secret_patterns.py`, shared with the push-time gate.

Fails closed: if git itself cannot be run (missing binary, locked index) the scan exits 2 and says it did **not** run, which blocks the commit — a check that quietly stops working is worse than one that is loudly broken.

Wired up as a pre-commit hook by `scripts/install_repo_hooks.py` (main-apiary) and by `core/git_hooks.py`, which `apiary install` calls for every other managed repo.

```bash
python scripts/secret_scan.py --staged             # what a commit would add
python scripts/secret_scan.py --path some/dir      # ad-hoc scan of a tree
python scripts/secret_scan.py --staged --entropy   # + high-entropy strings
```

| Flag | Description |
|------|-------------|
| `--staged` | Scan the staged diff (pre-commit mode) |
| `--path PATH` | Scan a file or directory tree instead |
| `--entropy` | Also flag high-entropy strings; noisier, off by default |
| `--quiet` | Print nothing on a clean scan |

Exit codes: `0` clean; `1` findings; `2` bad arguments, not a git repo, or the scan could not run.

False positives have three escape hatches, in order of preference: an inline `apiary:allow-secret` comment on the offending line; an entry in the repo-root `.secretsallow` file (a plain regex exempts every file whose path matches; `line:<regex>` exempts matching lines instead); or `git commit --no-verify`, which skips every pre-commit hook. See [config files](config-files.md#secretsallow).

## scripts/install_git_hooks.py

Install the secret-scan pre-commit hook into the **current** repo. Sibling of `install_repo_hooks.py`, which targets main-apiary's own checkout and installs the combined doc-check + secret-scan hook; this one targets any other apiary-managed repo.

Thin CLI over `core/git_hooks.py`. `apiary install` calls that module on every bootstrap, so use this by hand only to retrofit a repo bootstrapped before that was wired in, or to inspect / remove an install.

```bash
python .claude/apiary/launch.py scripts/install_git_hooks.py
python .claude/apiary/launch.py scripts/install_git_hooks.py --list
python .claude/apiary/launch.py scripts/install_git_hooks.py --uninstall
```

| Flag | Description |
|------|-------------|
| `--uninstall` | Remove the hook, if we own it |
| `--list` | Report install status without changing anything |
| `--force` | Replace an existing non-apiary pre-commit hook |
| `--repo PATH` | Target repo (default: the git repo containing the working directory) |

Exit codes: `0` success or nothing to do; `1` refused (foreign hook in the way, main-apiary targeted, or not a git repo); `2` bad arguments.

A pre-commit hook that isn't ours is never clobbered — inspect it first, then re-run with `--force`.

## scripts/probe_permission_prompt.py

Empirical check that apiary hooks still let default-mode permission prompts happen. Runs a headless `claude -p` in a bootstrapped repo in `manual` mode, asks for one unlisted Bash command (`python -c`, which is neither auto-approved nor built-in-protected) and reads `permission_denials` from the JSON result. Run it before and after any change to hook responses (`core/hook_context.py`, the dispatcher). Costs one short Haiku call.

```bash
poetry run python scripts/probe_permission_prompt.py /path/to/bootstrapped-repo
poetry run python scripts/probe_permission_prompt.py /path/to/bootstrapped-repo --model claude-haiku-4-5-20251001 --timeout 180
```

| Flag | Description |
|------|-------------|
| `--model MODEL` | Model for the probe call (default: Haiku) |
| `--timeout SECONDS` | Kill the probe after this many seconds (default: 180) |

Exit codes: `0` the call was held for a prompt (hooks are not auto-approving); `1` the call ran without a prompt (something voted allow); `2` inconclusive; `3` the probe itself could not run (no `claude`, timeout, non-JSON output).

## scripts/migrate_frontmatter.py

Reconciles the frontmatter already on disk with the one dialect in `core/frontmatter.py`. Phase 3.3 replaced five hand-rolled `---` parsers with a single module; this answers the question that follows a swap like that — **does every existing file still parse to the same thing?**

It walks a state dir by family (scribe learnings, scribe templates, memory files, research entries, capture sidecars), parses each file twice — once with a frozen copy of the parser that owned it before the swap, once with `core.frontmatter` — and reports the files where the two disagree. `backup/` snapshot directories are never walked.

`--check` is the default and writes nothing. `--apply` rewrites a file only when all four hold: the legacy and new parses are identical, the rewrite round-trips back to the same `(meta, body)`, the body is preserved byte-for-byte, and the file actually changes. Rewrites are cosmetic — quoting, list style, spacing — so `--apply` is optional housekeeping, not a prerequisite. Anything that disagrees is reported and skipped.

```bash
python scripts/migrate_frontmatter.py --check
python scripts/migrate_frontmatter.py --check --state-dir /path/to/.repos --verbose
python scripts/migrate_frontmatter.py --apply --family learnings
```

| Flag | Description |
|------|-------------|
| `--check` | Report differences without writing (default) |
| `--apply` | Rewrite files whose legacy and new parses agree |
| `--state-dir DIR` | Store to walk (default: `<repo>/.repos`) |
| `--family NAME` | Limit to `learnings`, `templates`, `memory`, `research`, or `captures`; repeatable |
| `--verbose` | List every file, not just the ones needing review |

Exit codes: `0` every file agrees (or every rewrite succeeded); `1` at least one file disagrees or could not be parsed; `2` the state dir does not exist.

## Test scripts

All tests use `unittest` and are run directly:

```bash
python budgeter/test_hooks.py
python scribe/test_notes.py
python docs/test_check_cli_claims.py
python harden/test_validators.py
python harden/test_assign_ids.py
python harden/test_validate_consolidation.py
python harden/test_validate_and_assign.py
python -m runner.test_orchestrator
```
