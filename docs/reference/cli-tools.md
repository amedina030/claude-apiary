---
type: reference
title: CLI Tools
scope: project
description: All Python CLI entry points with subcommands, flags, and usage examples
framework_version: "1.0"
last_verified: "2026-04-05"
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
| `--last N` | list | Show last N notes |
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

## budgeter/log_agent_cost.py

Log background agent token costs. Reads `<usage>` XML from stdin.

```bash
echo '<usage>...</usage>' | python budgeter/log_agent_cost.py --session-id ID [--agent NAME] [--cwd DIR]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--session-id ID` | yes | Current session ID |
| `--agent NAME` | no | Agent name (e.g. "startup") |
| `--cwd DIR` | no | Working directory for config resolution |

## clarifier/log_cost.py

Track and finalize clarifier session costs.

### Subcommands

| Subcommand | Usage | Description |
|------------|-------|-------------|
| `tally` | `log_cost.py tally --id UUID --tokens N --tools N --duration N` | Accumulate costs for one invocation |
| `finalize` | `log_cost.py finalize --id UUID --log FILE --prompt "..." [--session-id ID] [--budgeter-tmp PATH]` | Write cost.log entry and clean up |

## clarifier/write_log.py

Manage clarifier session log files. Reads JSON from stdin or file argument.

```bash
python clarifier/write_log.py [file]           # init new session
python clarifier/write_log.py --append [file]   # append round
python clarifier/write_log.py --complete [file]  # finalize session
```

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

## harden/pipeline.py

Combined validate + assign-IDs pipeline. Preferred over calling validators and assign_ids separately.

```bash
echo '<json>' | python harden/pipeline.py findings [--check-files] [--deep] [--sanitize]
python harden/pipeline.py findings --file findings.json [--check-files] [--deep] [--sanitize]
python harden/pipeline.py response --file response.json --expected-ids ATK-001,ATK-002 [--check-files]
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
| `--with-test-suite` | Also install clarifier test fixtures (global only) |

## pipeline/run.py

End-to-end pipeline orchestrator. Sequences all 6 stages, passes artifact paths via UUID convention, stops on any stage failure.

```bash
python pipeline/run.py pipeline/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `intake_path` | yes | Path to intake JSON file (`pipeline/intake/<uuid>.json`) |

Stages run in order: validate_intake → auto_refine → auto_plan → executor → auto_harden → approval. Each stage's input path is derived from the UUID. Prints per-stage status and elapsed time. Exit 0 if all stages pass; exit 1 on first failure.

Stage timeout is configurable via `pipeline/config.json` under `orchestrator.stage_timeout` (default: 3600s).

## pipeline/create_intake.py

Create an intake file for the autonomous pipeline. Generates a UUID-keyed JSON at `pipeline/intake/<uuid>.json`.

```bash
python pipeline/create_intake.py --title "Add caching" --problem "Repeated DB queries" --description "Add Redis cache layer" --scope "api/cache.py"
python pipeline/create_intake.py --from-todo 42
```

| Flag | Required | Description |
|------|----------|-------------|
| `--title TEXT` | yes* | Short title for the task |
| `--problem TEXT` | yes* | Problem statement (min 20 chars) |
| `--description TEXT` | yes* | Detailed description (min 20 chars) |
| `--scope TEXT` | yes* | What's in scope for this pipeline run |
| `--context TEXT` | no | Additional context (optional) |
| `--from-todo ID` | no | Scribe TODO ID to seed from (replaces manual fields) |

\* Required unless `--from-todo` is used.

## pipeline/validate_intake.py

Validate an intake JSON file. Checks required fields, types, minimum content thresholds, and ISO date format.

```bash
python pipeline/validate_intake.py pipeline/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to intake JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## pipeline/auto_refine.py

Autonomous refiner — Stage 2. Reads a validated intake JSON, launches a Claude Code subprocess to explore the codebase and produce a structured spec.

```bash
python pipeline/auto_refine.py pipeline/intake/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `intake` | yes | Path to intake JSON file |

Output: `pipeline/specs/<uuid>.json`. Model and retries configurable via `pipeline/config.json` under `refine`.

## pipeline/validate_spec.py

Validate a spec JSON file against the 8 handoff validation rules.

```bash
python pipeline/validate_spec.py pipeline/specs/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to spec JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## pipeline/auto_plan.py

Autonomous planner — Stage 3. Reads a validated spec JSON, launches a Claude Code subprocess to produce a step-by-step implementation plan.

```bash
python pipeline/auto_plan.py pipeline/specs/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `spec` | yes | Path to spec JSON file |

Output: `pipeline/plans/<uuid>.json`. Model and retries configurable via `pipeline/config.json` under `plan`.

## pipeline/validate_plan.py

Validate a plan JSON file for the autonomous pipeline.

```bash
python pipeline/validate_plan.py pipeline/plans/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `file` | yes | Path to plan JSON file |

Exit 0 on valid. Exit 1 with error details on invalid.

## pipeline/executor.py

Executor — Stage 4. Reads a validated plan JSON, creates a feature branch (`pipeline/<uuid>`), and executes each step via Claude Code subprocess.

```bash
python pipeline/executor.py pipeline/plans/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `plan` | yes | Path to plan JSON file |

Output: `pipeline/executions/<uuid>.json`. Creates branch `pipeline/<uuid>`. Model and retries configurable via `pipeline/config.json` under `executor`.

**Edge case:** Fails if branch `pipeline/<uuid>` already exists (not idempotent for re-runs).

## pipeline/auto_harden.py

Autonomous hardener — Stage 5. Runs attack-defend rounds against the executor's code changes using the existing `harden/` infrastructure.

```bash
python pipeline/auto_harden.py pipeline/executions/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `execution_log` | yes | Path to execution log JSON |

Output: `pipeline/hardens/<uuid>.json`. Rounds, models, and timeout configurable via `pipeline/config.json` under `harden` (default: 1 round).

## pipeline/approval.py

Approval — Stage 6. Reads the harden verdict and either auto-merges (all resolved), flags for review (unresolved findings), or rejects. Includes a deferral review sub-step that uses Claude to evaluate deferred findings.

```bash
python pipeline/approval.py pipeline/hardens/<uuid>.json
```

| Argument | Required | Description |
|----------|----------|-------------|
| `harden_result` | yes | Path to harden result JSON |

Output: `pipeline/reports/<uuid>.json`. Verdicts: `auto-merged`, `pending-review`, or stage failure.

## pipeline/draft_ticket.py

Create a backlog draft ticket. Writes a JSON to `pipeline/backlog/<slug>.json` and appends a `backlog` row to `pipeline/board.md`. Slug is derived from the title.

```bash
python pipeline/draft_ticket.py --title "..." --problem "..." --description "..." --scope "..."
python pipeline/draft_ticket.py --from-todo 42 --title "..." --problem "..." --scope "..."
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

## pipeline/promote.py

Promote a backlog draft to a pipeline intake file. Validates against the intake schema, assigns a UUID, copies to `pipeline/intake/<uuid>.json`, removes the backlog file, and updates `pipeline/board.md` status from `backlog` to `ready`.

```bash
python pipeline/promote.py <slug>
```

| Argument | Required | Description |
|----------|----------|-------------|
| `slug` | yes | Backlog ticket slug — the filename **without** directory or `.json` extension |

**Gotcha:** Pass the slug only (e.g. `my-feature`), not a path (e.g. `pipeline/backlog/my-feature.json`). Path separators are rejected to prevent traversal. The script always looks in `pipeline/backlog/<slug>.json`.

## pipeline/cost_emit.py

Shared helper used by every stage's `run_claude` wrapper. Library module — not a CLI tool. Parses a `claude -p --output-format json` envelope and emits a `<usage>` XML block to stderr that the orchestrator scrapes for cost tracking.

```python
from cost_emit import emit_usage_xml
emit_usage_xml(claude_subprocess_stdout)  # writes <usage>...</usage> to stderr
```

Silent on any failure — cost logging never breaks a stage. Sums all numeric fields under the envelope's `usage` key (input + output + cache_*) into a single `total_tokens` value.

## pipeline/config_loader.py

Shared config loader. Library module — not a CLI tool. Used by pipeline stages to read `pipeline/config.json`.

```python
from config_loader import get as cfg
timeout = cfg("orchestrator", "stage_timeout", 3600)
```

Falls back to defaults if `pipeline/config.json` is missing.

## Test scripts

All tests use `unittest` and are run directly:

```bash
python budgeter/test_hooks.py
python scribe/test_notes.py
python clarifier/test_write_log.py
python clarifier/test_log_cost.py
python harden/test_validators.py
python harden/test_assign_ids.py
python pipeline/test_orchestrator.py
```
