---
type: reference
title: Config Files
scope: project
description: All configuration files, their location, format, and editable fields
framework_version: "1.0"
last_verified: 2026-04-02
---

# Config Files

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
