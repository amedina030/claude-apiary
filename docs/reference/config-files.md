---
type: reference
title: Config Files
scope: project
description: All configuration files, their location, format, and editable fields
framework_version: "1.0"
last_verified: 2026-08-26
---

# Config Files

## runner/config.json

Runner stage settings. Located in the repo at `runner/config.json`. Every caller
reads it through `runner/config_loader.py`'s `get(section, key, default)`, so a
key the file omits falls back to the default the caller passes — the file is
optional in full.

The Section / Field / Type / Default columns below are **generated from the
shipped `runner/config.json`** by `docs/generate_reference.py`; only the
Description column is hand-written. Change a value in the file and
`--check` fails until the table is regenerated.

<!-- generated:start: config:runner/config.json -->
| Section | Field | Type | Default | Description |
|-------|-----|----|-------|-----------|
| `refine` | `max_retries` | int | `3` | Retries the refiner gets to produce a valid spec |
| `refine` | `model` | string | `"opus"` | Claude model alias for the refiner stage |
| `refine` | `timeout` | int | `900` | Per-attempt timeout in seconds |
| `plan` | `max_retries` | int | `3` | Retries the planner gets to produce a valid plan |
| `plan` | `model` | string | `"opus"` | Claude model alias for the planner stage |
| `plan` | `timeout` | int | `900` | Per-attempt timeout in seconds |
| `executor` | `model` | string | `"sonnet"` | Claude model alias for the executor stage |
| `executor` | `max_retries_per_step` | int | `2` | Retries per execution step |
| `executor` | `max_no_change_retries` | int | `2` | Retries allowed when a step returns without touching a file — the "the model did nothing" guard |
| `executor` | `timeout` | int | `900` | Per-step timeout in seconds |
| `executor` | `mode` | string | `"per_step"` | `per_step` runs one Claude call per plan step; `monolithic` runs the whole plan in one call (and uses `monolithic_executor.timeout_seconds`) |
| `monolithic_executor` | `timeout_seconds` | int | `1800` | Timeout for the single call the monolithic executor makes |
| `harden` | `max_rounds` | int | `1` | Attack-defend rounds per run |
| `harden` | `attacker_model` | string | `"opus"` | Claude model alias for the attacker |
| `harden` | `defender_model` | string | `"sonnet"` | Claude model alias for the defender |
| `harden` | `timeout` | int | `300` | Per-round timeout in seconds |
| `orchestrator` | `stage_timeout` | int | `3600` | Wall-clock ceiling for any one stage before the orchestrator kills it |
| `detached` | `token_cap` | int | `10000000` | Per-run token cap in detached (cron) mode; `--token-cap` overrides it |
| `detached` | `max_unreviewed` | int | `5` | Detached runs refuse to start a new ticket once this many branches are waiting for review |
| `detached` | `max_restarts` | int | `3` | How many times a detached run may be restarted after a recoverable failure |
| `runner` | `target_repo` | null | `null` | Default target repo path for runs that name none. `null` means "the repo apiary resolved" |
| `runner` | `banned_tokens` | object | `{"pytest": "use unittest (stdlib) \u2014 see docs/standards/code-style.md", "shell=true": "shell=True is banned \u2014 use list-form subprocess args", "import requests": "external dependencies are banned \u2014 stdlib only", "from requests": "external dependencies are banned \u2014 stdlib only"}` | Lowercase substring → the message the executor prints when a generated diff contains it |
| `runner` | `target_overrides` | object | `{}` | Target-repo path → a partial config that shadows the top-level one for runs against that repo |
| `usher` | `max_files` | object | `{"pass": 5, "warn": 8}` | Ticket-size gate: files touched, `pass`/`warn` thresholds |
| `usher` | `max_subsystems` | object | `{"pass": 2, "warn": 3}` | Ticket-size gate: distinct subsystems touched |
| `usher` | `max_description_chars` | object | `{"pass": 2000, "warn": 4000}` | Ticket-size gate: description length |
<!-- generated:end: config:runner/config.json -->

`runner.banned_tokens` maps a lowercase substring to the message the executor
prints when a generated diff contains it; `runner.target_overrides` maps a
target-repo path to a partial config that shadows the top-level one for runs
against that repo.

## cron_registry/&lt;hostname&gt;.json

Canonical list of scheduled OS-scheduler entries that apiary owns on a given machine. Located at `<apiary-repo>/cron_registry/<hostname>.json` where `<hostname>` is `platform.node()` (sanitised for filesystem use). Each machine has its own file so multi-machine git-sync setups don't collide. Read by `runner/cron_health.py` (`check` and `repair` subcommands).

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

## compass/config.json

Compass runtime config, controlling the live A/B on profile injection. Located at `compass/config.json` (in the repo — source, not state). `$APIARY_COMPASS_CONFIG` points at an alternate file; tests use it.

<!-- generated:start: config:compass/config.json -->
| Field | Type | Default | Description |
|-----|----|-------|-----------|
| `ab_enabled` | bool | `false` | When false (shipped default) **nothing changes**: every session is in arm `on` and `personality.md` is injected as always. Set true to start splitting sessions |
| `ab_seed` | string | `"compass-ab-2026-08"` | Salt for the per-session coin flip. Change it only to start a fresh measurement window — changing it mid-window makes the two halves incomparable |
| `ab_on_fraction` | float | `0.5` | Share of sessions in the injected arm. `0` sends everyone to `off`, `1` to `on` |
<!-- generated:end: config:compass/config.json -->

A malformed or missing file reads as the defaults (A/B off) — this is loaded from a startup hook and must never break session start. See [Compass Measurement Programme](../architecture/compass-measurement.md).

## compass/label_vocabulary.json

Target definition for `compass/evaluate.py offline`: `{"labels": {<dimension>: {<label>: [cue, ...]}}}`. The labels are the poles named in that dimension's `description` in `dimensions.json`; the cues are case-insensitive substrings counted over an observation's `observation` text. Editing this file changes the metric — do it before a measurement window, never inside one. `compass/evaluate.py labels` prints the current vocabulary.

## budgeter/config.json

Global budgeter configuration. Located in the repo at `budgeter/config.json`.
`monitored_tools` is also what `core/hooks/dispatch.py` turns into the
budgeter hooks' matcher, so changing it changes which tool calls are logged
*and* which ones pay for the hook.

The last four rows are read by `budgeter/report.py --weighted` and are
deliberately **absent from the shipped file** — their defaults live in
`budgeter/report.py` and are generated from there.

<!-- generated:start: config:budgeter/config.json -->
| Field | Type | Default | Description |
|-----|----|-------|-----------|
| `monitored_tools` | array | `["Agent", "Bash", "Read", "Write"]` | Tool names the budgeter hooks fire for; also the dispatcher matcher for both budgeter hooks |
| `session_warn_soft_tokens` | int | `600000` | Prompt size at which the session-length nudge suggests wrapping up |
| `session_warn_hard_tokens` | int | `800000` | Prompt size at which it suggests starting a new session now |
| `price_weight_input` | float | `1.0` (code default; not in the file) | Weight applied to input tokens by `report.py --weighted` |
| `price_weight_cache` | float | `0.1` (code default; not in the file) | Weight applied to cache-read tokens |
| `price_weight_cache_creation` | float | `1.25` (code default; not in the file) | Weight applied to cache-creation tokens |
| `price_weight_output` | float | `5.0` (code default; not in the file) | Weight applied to output tokens |
<!-- generated:end: config:budgeter/config.json -->

## .claude/budgeter.json (per-project)

Optional per-project budgeter override. Hand-authored — the global install flow that created it was retired in the per-repo migration. Same schema as `budgeter/config.json`. `logger.load_config` reads whichever single file applies — there is no merge with `budgeter/config.json`, so a per-project file must restate every key it wants. Its presence also redirects the log and baseline paths into `<project>/.claude/`.

## .claude/settings.json

Claude Code settings file at `<repo>/.claude/settings.json`. Hook entries are generated by `core/install.py` (via `core/hooks_factory.py`) — do not edit them manually.

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
