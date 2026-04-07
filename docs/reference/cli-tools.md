---
type: reference
title: CLI Tools
scope: project
description: All Python CLI entry points with subcommands, flags, and usage examples
framework_version: "1.0"
last_verified: "2026-04-07"
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
| `get` | `notes.py get <id>` | Show a single note by ID |
| `done` | `notes.py done <id>` | Mark a note as done |
| `update` | `notes.py update <id> --content "<text>"` | Update note content |
| `archive` | `notes.py archive [--before YYYY-MM-DD]` | Archive old notes |
| `learn` | `notes.py learn --content "<text>"` | Add a learning |
| `learnings` | `notes.py learnings` | List all learnings |
| `unlearn` | `notes.py unlearn <id>` | Remove a learning |
| `handoff-sessions` | `notes.py handoff-sessions` | List sessions with handoffs |
| `migrate` | `notes.py migrate` | Run data migrations |

### Common flags

| Flag | Applies to | Description |
|------|-----------|-------------|
| `--project PROJECT` | all | Project key override (default: derived from cwd) |
| `--type TYPE` | add, list | Note type: `todo`, `handoff`, `decision`, `wishlist`, `reference`, `blocker`, `context` |
| `--content TEXT` | add, update, learn | Note/learning content |
| `--session-id ID` | add, update, learn | Associate with a session |
| `--auto` | add | Mark as auto-generated |
| `--if-no-handoff-for ID` | add | Only add if no handoff exists for this session |
| `--full` | learnings | Print full content (not truncated) |
| `--search TEXT` | list, learnings | Full-text search |
| `--last N` / `--limit N` | list | Show last N notes (both spellings accepted as aliases) |
| `--all` | list | Include done notes |
| `--archive` | list | Search archive instead of active |
| `--role ROLE` | add, list, learn | Session role filter |
| `--mission MISSION` | add, list, learn | Session mission filter |
| `--before DATE` | archive | Archive notes before this date |

## core/startup.py

Session initialization and summary loading.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `init` | `startup.py init --session-id ID --first-message "..." --repo-dir PATH` | Initialize session, detect unseen transcripts |
| `summary` | `startup.py summary [--repo-dir PATH] [--role R] [--mission M]` | Load active notes and learnings summary |

### Flags

| Flag | Applies to | Required | Description |
|------|-----------|----------|-------------|
| `--session-id ID` | init | yes | Current session ID (first 8 chars) |
| `--first-message TEXT` | init | yes | User's first message |
| `--repo-dir PATH` | init, summary | init: yes | Repository root directory |
| `--role ROLE` | summary | no | Filter by role |
| `--mission MISSION` | summary | no | Filter by mission |

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

## runner/run.py

End-to-end runner orchestrator. Sequences all 6 stages, passes artifact paths via UUID convention, stops on any stage failure.

```bash
python runner/run.py runner/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `intake_path` | yes | Path to intake JSON file (`runner/intake/<uuid>.json`) |

Stages run in order: validate_intake → auto_refine → auto_plan → executor → auto_harden → approval. Each stage's input path is derived from the UUID. Prints per-stage status and elapsed time. Exit 0 if all stages pass; exit 1 on first failure.

Stage timeout is configurable via `runner/config.json` under `orchestrator.stage_timeout` (default: 3600s).

## runner/create_intake.py

Create an intake file for the autonomous runner. Generates a UUID-keyed JSON at `runner/intake/<uuid>.json`.

```bash
python runner/create_intake.py --title "Add caching" --problem "Repeated DB queries" --description "Add Redis cache layer" --scope "api/cache.py"
python runner/create_intake.py --from-todo 42
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

## runner/validate_intake.py

Validate an intake JSON file. Checks required fields, types, minimum content thresholds, and ISO date format.

```bash
python runner/validate_intake.py runner/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to intake JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## runner/auto_refine.py

Autonomous refiner — Stage 2. Reads a validated intake JSON, launches a Claude Code subprocess to explore the codebase and produce a structured spec.

```bash
python runner/auto_refine.py runner/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `intake` | yes | Path to intake JSON file |

Output: `runner/specs/<uuid>.json`. Model and retries configurable via `runner/config.json` under `refine`.

## runner/validate_spec.py

Validate a spec JSON file against the 8 handoff validation rules.

```bash
python runner/validate_spec.py runner/specs/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to spec JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## runner/auto_plan.py

Autonomous planner — Stage 3. Reads a validated spec JSON, launches a Claude Code subprocess to produce a step-by-step implementation plan.

```bash
python runner/auto_plan.py runner/specs/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `spec` | yes | Path to spec JSON file |

Output: `runner/plans/<uuid>.json`. Model and retries configurable via `runner/config.json` under `plan`.

## runner/validate_plan.py

Validate a plan JSON file for the autonomous runner.

```bash
python runner/validate_plan.py runner/plans/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to plan JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## runner/executor.py

Executor — Stage 4. Reads a validated plan JSON, creates a feature branch (`runner/<uuid>`), and executes each step via Claude Code subprocess.

```bash
python runner/executor.py runner/plans/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `plan` | yes | Path to plan JSON file |

Output: `runner/executions/<uuid>.json`. Creates branch `runner/<uuid>`. Model and retries configurable via `runner/config.json` under `executor`.

**Edge case:** Fails if branch `runner/<uuid>` already exists (not idempotent for re-runs).

## runner/auto_harden.py

Autonomous hardener — Stage 5. Runs attack-defend rounds against the executor's code changes using the existing `harden/` infrastructure.

```bash
python runner/auto_harden.py runner/executions/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `execution_log` | yes | Path to execution log JSON |

Output: `runner/hardens/<uuid>.json`. Rounds, models, and timeout configurable via `runner/config.json` under `harden` (default: 1 round).

## runner/approval.py

Approval — Stage 6. Reads the harden verdict and either auto-merges (all resolved), flags for review (unresolved findings), or rejects. Includes a deferral review sub-step that uses Claude to evaluate deferred findings.

```bash
python runner/approval.py runner/hardens/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `harden_result` | yes | Path to harden result JSON |

Output: `runner/reports/<uuid>.json`. Verdicts: `auto-merged`, `pending-review`, or stage failure.

## runner/draft_ticket.py

Create a backlog draft ticket. Writes a JSON to `runner/backlog/<slug>.json` and appends a `backlog` row to `runner/board.md`. Slug is derived from the title.

```bash
python runner/draft_ticket.py --title "..." --problem "..." --description "..." --scope "..."
python runner/draft_ticket.py --from-todo 42 --title "..." --problem "..." --scope "..."
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

Promote a backlog draft to a runner intake file. Validates against the intake schema, assigns a UUID, copies to `runner/intake/<uuid>.json`, removes the backlog file, and updates `runner/board.md` status from `backlog` to `ready`.

```bash
python runner/promote.py <slug>
```

| Argument | Required | Description |
|----------|----------|-------------|
| `slug` | yes | Backlog ticket slug — the filename **without** directory or `.json` extension |

**Gotcha:** Pass the slug only (e.g. `my-feature`), not a path (e.g. `runner/backlog/my-feature.json`). Path separators are rejected to prevent traversal. The script always looks in `runner/backlog/<slug>.json`.

## runner/mark_done.py

Mark a backlog ticket as done **without** running it through the runner. For tickets small enough to fix by hand. Deletes `runner/backlog/<slug>.json` and rewrites the matching `runner/board.md` row's status column to `done`.

```bash
python runner/mark_done.py <slug> [--note "explanation"]
```

| Argument / Flag | Required | Description |
|-----------------|----------|-------------|
| `slug` | yes | Backlog ticket slug — the filename **without** directory or `.json` extension |
| `--note TEXT` | no | Optional note appended to the board row's notes column (e.g. "hand-fixed manually, not via runner") |

**Refuses to operate** if the board row is not in `backlog` status — guards against clobbering runner-run state (`ready`, `running`, `failed`, `done`). Use `promote.py` first if you actually want the ticket to run through the runner.

## runner/cost_emit.py

Shared helper used by every stage's `run_claude` wrapper. Library module — not a CLI tool. Parses a `claude -p --output-format json` envelope and emits a `<usage>` XML block to stderr that the orchestrator scrapes for cost tracking.

```python
from cost_emit import emit_usage_xml
emit_usage_xml(claude_subprocess_stdout)  # writes <usage>...</usage> to stderr
```

Silent on any failure — cost logging never breaks a stage. Sums all numeric fields under the envelope's `usage` key (input + output + cache_*) into a single `total_tokens` value.

## runner/config_loader.py

Shared config loader. Library module — not a CLI tool. Used by runner stages to read `runner/config.json`.

```python
from config_loader import get as cfg
timeout = cfg("orchestrator", "stage_timeout", 3600)
```

Falls back to defaults if `runner/config.json` is missing.

## Test scripts

All tests use `unittest` and are run directly:

```bash
python budgeter/test_hooks.py
python scribe/test_notes.py
python harden/test_validators.py
python harden/test_assign_ids.py
python runner/test_orchestrator.py
```
