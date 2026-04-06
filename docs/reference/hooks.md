---
type: reference
title: Hooks
scope: project
description: All registered hooks, their lifecycle events, and what each does
framework_version: "1.0"
last_verified: 2026-04-05
---

# Hooks

Hooks are Python scripts registered in `~/.claude/settings.json` that fire at Claude Code lifecycle events. They run as shell commands — no token cost.

## Hook lifecycle events

| Event | When it fires |
|-------|--------------|
| **PreToolUse** | Before every tool call |
| **PostToolUse** | After every tool call |
| **Stop** | When the session ends |

## Registered hooks

### Budgeter hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Pre-tool cost logger + warning | PreToolUse | `budgeter/hooks/pre_tool_use.py` | Logs the previous tool call's token cost (PRE-to-PRE delta), evaluates whether the upcoming response looks expensive, injects warning if threshold exceeded |
| Post-tool agent logger | PostToolUse | `budgeter/hooks/post_tool_use.py` | Logs exact subagent token cost from `tool_response.totalTokens` (Agent calls only) |
| Stop session cleanup | Stop | `budgeter/hooks/stop_session.py` | Logs the final tool call's cost, cleans up temp baseline files |

### Core hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Install checker | PreToolUse | `core/hooks/check_install.py` | Verifies installed files match repo manifest. Runs once per session (sets a flag, skips subsequent calls) |
| Install checker cleanup | Stop | `core/hooks/check_install_stop.py` | Removes the "already checked" flag file |
| Session injector | PreToolUse | `core/hooks/inject_session.py` | Injects session identity (session_id, role, mission) into hook context |
| Transcript saver | Stop | `core/hooks/save_transcript.py` | Saves a stripped copy of the session transcript for handoff generation |
| Startup context injector | UserPromptSubmit | `core/hooks/startup_prompt_hook.py` | Injects identity, notes summary, learnings, and CLI reference on the first user message |
| Unseen session detector | PreToolUse | `core/hooks/startup_hook.py` | Detects unseen session transcripts on first tool call (gated by `auto-startup` flag) |

## Hook execution order

All PreToolUse hooks fire before every tool call. The order is determined by their position in `settings.json` (managed by `setup.py`). Current order:

1. `inject_session.py` — adds session context
2. `check_install.py` — validates installation (first call only)
3. `pre_tool_use.py` — logs cost, checks for expensive operations
4. `remind_standards.py` — reminds to consult standards (Write/Edit only, once per category per session)

PostToolUse hooks fire after a tool returns:

1. `post_tool_use.py` — logs agent costs

Stop hooks fire on session end:

1. `stop_session.py` — logs final cost, cleans temp files
2. `check_install_stop.py` — cleans flag file
3. `save_transcript.py` — saves transcript

### Docs hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Standards reminder | PreToolUse | `docs/hooks/remind_standards.py` | On Write/Edit of `.py` or `docs/*.md` files, injects a one-line reminder to consult the relevant standards doc. Once per file category per session. |

### Project hooks

Registered via the project-level `.claude/settings.json` (not user-global), so they only fire when Claude Code runs inside this repo.

| Hook | Event | File | Description |
|------|-------|------|-------------|
| CLI lookup enforcer | PreToolUse | `hooks/enforce_cli_lookup.py` | On Bash, blocks invocations of known repo CLI tools (from `cli-tools.md`) unless `cli_lookup.py` was run for that tool earlier in the session transcript. Exempts `cli_lookup.py` itself. Fails open on internal error. |

## Utility scripts in hooks directories

These scripts live under `hooks/` directories but are not registered as Claude Code hooks. They are invoked directly by other tools.

| Script | File | Called by | Description |
|--------|------|-----------|-------------|
| Transcript extractor | `core/hooks/extract_transcript.py` | Startup agent | Extracts user + assistant messages from raw transcript JSONL. Usage: `python extract_transcript.py <path>` |

## Hook registration

Hooks are registered by `setup.py --global`. Each hook entry in `settings.json` specifies:
- `matcher`: which tool types trigger this hook (e.g. `Agent`, `Bash`, or `*` for all)
- `hooks`: array of `{type, command}` objects

See `core/hooks_lib.py` for the registration API.
