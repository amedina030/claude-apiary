---
type: reference
title: CLI Tools
scope: project
description: All Python CLI entry points with subcommands, flags, and usage examples
framework_version: "1.0"
last_verified: 2026-04-03
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
| `--search TEXT` | list | Full-text search |
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

## Test scripts

All tests use `unittest` and are run directly:

```bash
python budgeter/test_hooks.py
python scribe/test_notes.py
python clarifier/test_write_log.py
python clarifier/test_log_cost.py
```
