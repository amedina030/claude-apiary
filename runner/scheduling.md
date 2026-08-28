# Runner Scheduling

How to get the runner executing tickets automatically. Two paths exist:
**remote triggers** (Claude Code `/schedule` skill) and **local cron**
(OS-level scheduler). Both invoke the same command.

## Ticket lifecycle

```
draft_ticket.py  -->  <state-dir>/runner/backlog/<slug>.json   (has UUID from draft time)
                          |
              +-----------+-----------+
              |                       |
        [detached picks it]     [promote.py <slug>]
        run.py --detached         |
              |              <state-dir>/runner/intake/<uuid>.json
              |                   |
              |             [interactive run]
              |             run.py <intake-path>
              |                   |
              +----->  runner branch  ----->  review / merge / cleanup
```

- `draft_ticket.py` creates a backlog JSON with a UUID already assigned.
  Both `--detached` mode and `promote.py` can consume it.
- `promote.py` validates, copies to `intake/`, and removes from `backlog/`.
  Use this for the interactive path (you supply the intake path to `run.py`).
- `--detached` mode picks the oldest unclaimed backlog item directly
  (oldest by mtime, skips items whose UUID already has a `runner/*` branch).

## The command

```
python -m runner.run --detached
```

Optional flags:

| Flag | Default | Effect |
|------|---------|--------|
| `--token-cap N` | `config.json` `detached.token_cap` (10000000) | Max tokens per run |
| `--max-unreviewed N` | `config.json` `detached.max_unreviewed` (5) | Skip if this many runner branches await review |
| `--intake <path>` | (picks from backlog) | Override backlog selection with a specific intake file |

## Path A: Remote trigger (recommended)

Uses Claude Code's `/schedule` skill to register a cloud-hosted cron trigger.

### Prerequisites

1. **Claude Code CLI** installed and authenticated.
2. **GitHub App connection** configured in your Claude Code account
   (the `/schedule` skill will prompt if missing).
3. **MCP connectors** enabled (required by remote triggers).
4. **`environment_id`** selected when prompted (ties the trigger to a
   specific compute environment).

### Setup

Open Claude Code in the repo root and run:

```
/schedule
```

When prompted, supply:
- **Command:** `python -m runner.run --detached`
- **Cron:** `0 2 * * *` (daily at 2 AM, or your preferred expression)
- **Working directory:** repo root (set automatically)

### Recommended cron expressions

| Expression | Schedule |
|-----------|----------|
| `0 2 * * *` | Daily at 2 AM |
| `0 1-5 * * *` | Hourly from 1-5 AM (spread load) |
| `0 2 * * 1-5` | Weekdays at 2 AM |

## Path B: Local cron / Task Scheduler

Run `python -m runner.run --detached` from your OS scheduler. The command
is identical; it just runs on your machine instead of in the cloud.

### Linux / macOS (crontab)

```bash
crontab -e
```

Add:

```
0 2 * * * cd /path/to/claude-apiary && python -m runner.run --detached >> runner/cron.log 2>&1
```

### Windows (Task Scheduler)

1. Open Task Scheduler, create a new task.
2. **Trigger:** daily at 2:00 AM.
3. **Action:** Start a program.
   - Program: `python`
   - Arguments: `-m runner.run --detached`
   - Start in: `D:\path\to\claude-apiary`
4. Enable "Run whether user is logged on or not" if desired.

### PowerShell (schtasks)

```powershell
schtasks /create /tn "Apiary Runner" /tr "python -m runner.run --detached" ^
  /sc daily /st 02:00 /sd (Get-Date -Format MM/dd/yyyy)
```

## Guardrail knobs

Edit `runner/config.json` before scheduling:

| Key | Default | Effect |
|-----|---------|--------|
| `detached.token_cap` | 10000000 | Max tokens Claude may spend per detached run |
| `detached.max_unreviewed` | 5 | Blocks new runs when this many runner branches await review |

## Morning review

After overnight runs, inspect pending branches:

```
python -m runner.queue
```

Lists all `runner/*` branches joined with `run_history.jsonl` entries.

## Cleanup

```bash
# Abort a specific run (delete branch + archive log)
python -m runner.run --cleanup <UUID>

# Prune old failed/aborted runs (default: older than 7 days)
python -m runner.run --prune-failed [--older-than DAYS] [--dry-run]
```
