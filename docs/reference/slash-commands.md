---
type: reference
title: Slash Commands
scope: project
description: All slash commands, what they do, and when to use them
framework_version: "1.0"
last_verified: 2026-04-02
---

# Slash Commands

Slash commands are defined in markdown files under `commands/` directories. Claude Code loads them at session start from `~/.claude/commands/`. They are invoked by typing `/<name>` in the Claude Code prompt.

## Command list

| Command | Source | Description |
|---------|--------|-------------|
| `/startup` | `core/commands/startup.md` | Session initialization — generates handoffs, loads notes and learnings |
| `/budgeter-log` | `budgeter/commands/budgeter-log.md` | Toggle token logging on/off |
| `/budgeter-warn` | `budgeter/commands/budgeter-warn.md` | Toggle cost estimation warnings on/off |
| `/budgeter-setup` | `budgeter/commands/budgeter-setup.md` | Set up budgeter for a specific project |
| `/clarifier` | `clarifier/commands/clarifier.md` | Toggle clarifier on/off |
| `/run-clarifier-tests` | `clarifier/commands/run-clarifier-tests.md` | Run clarifier test suite (24 automated cases) |
| `/note` | `scribe/commands/note.md` | Add a typed note (type auto-detected from prefix) |
| `/notes` | `scribe/commands/notes.md` | List and query notes |
| `/review` | `docs/commands/review.md` | Review changes against standards, then fix issues |

## Toggles

These commands create or remove flag files at `~/.claude/<name>-enabled`:

| Command | Flag file | Default |
|---------|-----------|---------|
| `/budgeter-log` | `~/.claude/budgeter-log-enabled` | off |
| `/budgeter-warn` | `~/.claude/budgeter-warn-enabled` | off |

Toggles persist across sessions.

## Always-active features

These features have no toggle — they're always on:

- **Scribe** (notes, learnings, handoffs) — `/note`, `/notes`
- **Session startup** — `/startup` runs automatically on first message per session
- **Install checker** — verifies installed files match repo on first tool call
- **Transcript saver** — saves session transcript on session end
