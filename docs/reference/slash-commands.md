---
type: reference
title: Slash Commands
scope: project
description: All slash commands, what they do, and when to use them
framework_version: "1.0"
last_verified: 2026-04-03
---

# Slash Commands

Slash commands are defined in markdown files under `commands/` directories. Claude Code loads them at session start from `~/.claude/commands/`. They are invoked by typing `/<name>` in the Claude Code prompt.

## Command list

| Command | Source | Description |
|---------|--------|-------------|
| `/backfill-handoffs` | `core/commands/backfill-handoffs.md` | Process unseen session transcripts into handoff notes |
| `/budgeter-log` | `budgeter/commands/budgeter-log.md` | Toggle token logging on/off |
| `/budgeter-warn` | `budgeter/commands/budgeter-warn.md` | Toggle cost estimation warnings on/off |
| `/budgeter-setup` | `budgeter/commands/budgeter-setup.md` | Set up budgeter for a specific project |
| `/clarifier` | `clarifier/commands/clarifier.md` | Toggle clarifier on/off |
| `/run-clarifier-tests` | `clarifier/commands/run-clarifier-tests.md` | Run clarifier test suite (24 automated cases) |
| `/note` | `scribe/commands/note.md` | Add a typed note (type auto-detected from prefix) |
| `/notes` | `scribe/commands/notes.md` | List and query notes |
| `/refine` | `refiner/commands/refine.md` | Refine a fuzzy idea into a structured handoff spec through value-first adversarial questioning |
| `/harden` | `harden/commands/harden.md` | Adversarial attack-defend loop that stress-tests code or plans |
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
- **Session startup** — context injected automatically via `UserPromptSubmit` hook (toggled by `auto-startup` flag); `/backfill-handoffs` processes unseen transcripts
- **Install checker** — verifies installed files match repo on first tool call
- **Transcript saver** — saves session transcript on session end
