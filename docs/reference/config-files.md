---
type: reference
title: Config Files
scope: project
description: All configuration files, their location, format, and editable fields
framework_version: "1.0"
last_verified: 2026-04-05
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

Optional per-project budgeter override. Created by `setup.py --project-path`. Same schema as `budgeter/config.json`. Loaded via `core/config.py` with `budgeter/config.json` as the defaults fallback.

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
