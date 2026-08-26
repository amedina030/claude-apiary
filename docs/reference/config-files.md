---
type: reference
title: Config Files
scope: project
description: All configuration files, their location, format, and editable fields
framework_version: "1.0"
last_verified: 2026-06-11
---

# Config Files

## runner/config.json

Runner stage settings. Located in the repo at `runner/config.json`. All values have built-in defaults — the file is optional.

| Section | Field | Type | Default | Description |
|---------|-------|------|---------|-------------|
| `refine` | `max_retries` | int | `3` | Max retries for spec generation |
| `refine` | `model` | string | `"opus"` | Claude model alias for refiner |
| `refine` | `timeout` | int | `300` | Per-retry timeout (seconds) |
| `plan` | `max_retries` | int | `3` | Max retries for plan generation |
| `plan` | `model` | string | `"opus"` | Claude model alias for planner |
| `plan` | `timeout` | int | `300` | Per-retry timeout (seconds) |
| `executor` | `model` | string | `"sonnet"` | Claude model alias for executor |
| `executor` | `max_retries_per_step` | int | `2` | Retries per execution step |
| `executor` | `timeout` | int | `300` | Per-step timeout (seconds) |
| `harden` | `max_rounds` | int | `1` | Attack-defend rounds |
| `harden` | `attacker_model` | string | `"opus"` | Attacker Claude model alias |
| `harden` | `defender_model` | string | `"sonnet"` | Defender Claude model alias |
| `harden` | `timeout` | int | `300` | Per-round timeout (seconds) |
| `orchestrator` | `stage_timeout` | int | `3600` | Max seconds per stage before kill |

Loaded by `runner/config_loader.py`: `get(section, key, default)`.

## cron_registry/&lt;hostname&gt;.json

Canonical list of scheduled OS-scheduler entries that apiary owns on a given machine. Located at `<apiary-repo>/cron_registry/<hostname>.json` where `<hostname>` is `platform.node()` (sanitised for filesystem use). Each machine has its own file so multi-machine git-sync setups don't collide. Read by `runner/cron_health.py` (`check` and `repair` subcommands) and by `scripts/bootstrap.py`'s tail-end drift report.

```json
{
  "entries": [
    {
      "id": "overnight-runner",
      "description": "Nightly detached runner pass",
      "schedule": {"type": "daily", "time": "02:00"},
      "command": ["<python>", "-m", "runner.run", "--detached"],
      "cwd": "<apiary_repo>"
    },
    {
      "id": "compass-weekly-synthesis",
      "description": "Weekly compass synthesis (self-throttled to 7-day cadence via --cron)",
      "schedule": {"type": "daily", "time": "03:00"},
      "command": ["<python>", "-m", "compass.synthesize", "--cron"],
      "cwd": "<apiary_repo>"
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Stable handle; becomes the suffix after the `\apiary\` scheduler prefix |
| `description` | string | no | Human-readable label shown in the `check` table |
| `schedule.type` | string | yes | `"daily"` is the only type implemented this release |
| `schedule.time` | string | yes | 24-hour `HH:MM` |
| `command` | list of strings | yes | List-form command; rendered with backend-specific quoting at register time. Use the `<python>` placeholder for the interpreter (resolves per-machine to a real Python 3, honoring `APIARY_PYTHON`) rather than a literal `python`/`python3`, which isn't present on every OS. Also supports `<apiary_repo>`. |
| `cwd` | string | no | Working directory; supports the `<apiary_repo>` placeholder |
| `disabled` | bool | no | `true` means the entry must NOT exist in the scheduler; `repair --apply` deletes any matching entry |

## &lt;main-apiary&gt;/.apiary/gui/apiary_gui/launch.json

Claude Code spawn configuration for the GUI. Auto-created on first run from `DEFAULT_LAUNCH` in `gui/theme.py`; hand-edit to persist non-default values.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | string | `"claude"` | Executable spawned as the pty subprocess. Typically `claude`; override only to point at a vendored binary |
| `args` | list of strings | `[]` | Extra flags appended to the spawn argv (before any flags the GUI adds itself, e.g. `--permission-mode acceptEdits` or `--mcp-config`) |
| `cwd` | string | `""` | Default working directory used when no saved tabs exist. Empty string shows the first-run picker |
| `rows` | int | `40` | Pty rows |
| `cols` | int | `120` | Pty cols |
| `permission_mcp` | bool | `false` | Route permission prompts through the structured MCP path (`gui/permission_mcp.py` + loopback HTTP bridge) instead of the TUI-banner scraper. Env `APIARY_PERMISSION_MCP` overrides this when set. See scribe `C-2026-36` |

Unknown top-level keys are dropped on load (whitelist, see `gui/theme.py::load_launch`). To add a field, update `DEFAULT_LAUNCH` first.

## compass/dimensions.json

List of personality dimensions the compass synthesizer extracts and emits. Located at `compass/dimensions.json` (in the repo, not under `.apiary/` — it's source, not state).

```json
{
  "dimensions": [
    {
      "name": "communication_style",
      "volatile": false,
      "description": "Verbosity, directness, formality of user messages..."
    },
    {
      "name": "mood_tone",
      "volatile": true,
      "description": "Current emotional state, energy..."
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `dimensions[].name` | string | yes | Snake_case dimension name. Must be unique within the file |
| `dimensions[].volatile` | bool | yes | `true` for current-state signals (mood, energy); `false` for stable traits |
| `dimensions[].description` | string | yes | One-paragraph description used by the capture and synthesis prompts |

Adding a dimension: edit the JSON, then re-run any backfill or wait for the next `/wrapup` and weekly synthesis to start populating it. Removing a dimension: existing observations with that dimension will fail validation; archive or delete them first.

## budgeter/config.json

Global budgeter configuration. Located in the repo at `budgeter/config.json`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `monitored_tools` | array | `["Agent", "Bash", "Read", "Write"]` | Tool types to track |
| `min_tasks` | int | `50` | Minimum unique tasks logged before warnings activate |
| `expensive_token_threshold` | int\|null | `null` | Hard token limit. Overrides percentile if set |
| `expensive_percentile` | int | `90` | Percentile of historical task costs for warning threshold |
| `similarity_top_n` | int | `10` | Number of similar past tasks to compare against |
| `scope_rules` | object | (see file) | Rule definitions for scope detection |
| `scope_weights` | object | (see file) | Weight per rule for scoring |
| `scope_threshold` | float | (see file) | Weighted score threshold for triggering a warning |

## .claude/budgeter.json (per-project)

Optional per-project budgeter override. Hand-authored — the historical `setup.py --project-path` install flow that created it was retired in the per-repo migration. Same schema as `budgeter/config.json`. Loaded via `core/config.py` with `budgeter/config.json` as the defaults fallback.

## .claude/settings.json

Claude Code settings file at `~/.claude/settings.json`. Managed by `setup.py` — do not edit hook entries manually.

Contains:
- `hooks` — registered PreToolUse, PostToolUse, and Stop hooks
- `permissions` — tool permission rules
- Other Claude Code settings

## core/config/session-registry.json

Registered session roles and missions. Used by `core/session.py` to validate session identity.

```json
{
  "roles": ["user", "attacker", ...],
  "missions": ["general", "project-x", ...]
}
```

## .claude-session-identity.json

Current session identity. Written by `/startup`, read by hooks. Located at repo root (git-ignored).

```json
{
  "session_id": "eaf53215",
  "role": "user",
  "mission": "general"
}
```

## .secretsallow

Per-repo allowlist for both secret-scanning gates — `scripts/secret_scan.py`
at commit time and `core/hooks/pre_push_secret_scan.py` at push time. Lives at
the repo root and is committed, so the exemption travels with the repo. One regex per line; blank
lines and lines starting with `#` are ignored. A plain entry is tested against
the repo-relative **path** and exempts that whole file; an entry prefixed
`line:` is tested against the offending **line** instead and exempts matching
lines anywhere. (Earlier versions tested every entry against both, so a loose
path regex silenced any line containing that word.) An invalid regex is
skipped with a warning rather than failing the scan.

```
# The scanner's own pattern table and fixtures are credential-shaped by
# definition; scanning them would block every commit that touches the feature.
^scripts/secret_scan\.py$
^scripts/test_secret_scan\.py$
```

Prefer the inline `apiary:allow-secret` pragma for a one-off line — an entry
here exempts an entire file from every pattern, which is a wider hole than it
looks. `git commit --no-verify` remains the last-resort bypass.
