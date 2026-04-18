---
type: reference
title: Slash Commands
scope: project
description: All slash commands, what they do, and when to use them
framework_version: "1.0"
last_verified: 2026-04-17
---

# Slash Commands

Slash commands are defined in markdown files under `commands/` directories. Claude Code loads them at session start from `~/.claude/commands/`. They are invoked by typing `/<name>` in the Claude Code prompt.

## Command list

| Command | Source | Description |
|---------|--------|-------------|
| `/apiary-context` | `core/commands/apiary-context.md` | Load apiary toolkit context (scribe, budgeter, runner, portability rules) |
| `/backfill-handoffs` | `core/commands/backfill-handoffs.md` | Process unseen session transcripts into handoff notes |
| `/budgeter-log` | `budgeter/commands/budgeter-log.md` | Toggle token logging on/off |
| `/budgeter-warn` | `budgeter/commands/budgeter-warn.md` | Toggle cost estimation warnings on/off |
| `/budgeter-session-warn` | `budgeter/commands/budgeter-session-warn.md` | Toggle session-length wrap-up nudge on/off |
| `/budgeter-setup` | `budgeter/commands/budgeter-setup.md` | Set up budgeter for a specific project |
| `/note` | `scribe/commands/note.md` | Add a typed note (type auto-detected from prefix) |
| `/notes` | `scribe/commands/notes.md` | List and query notes |
| `/refine` | `refiner/commands/refine.md` | Refine a fuzzy idea into a structured handoff spec through value-first adversarial questioning |
| `/harden` | `harden/commands/harden.md` | Adversarial attack-defend loop that stress-tests code or plans |
| `/review` | `docs/commands/review.md` | Review changes against standards, then fix issues |
| `/wrapup` | `core/commands/wrapup.md` | Commit, push, and generate a session handoff note. Also captures compass observations from the session (Step 4, non-blocking). |
| `/compass-sync` | `compass/commands/compass-sync.md` | Manually trigger compass synthesis — regenerate `personality.md` from active observations |

## Toggles

These commands create or remove flag files at `~/.claude/<name>-enabled`:

| Command | Flag file | Default |
|---------|-----------|---------|
| `/budgeter-log` | `~/.claude/budgeter-log-enabled` | off |
| `/budgeter-warn` | `~/.claude/budgeter-warn-enabled` | off |
| `/budgeter-session-warn` | `~/.claude/budgeter-session-warn-enabled` | off |

Toggles persist across sessions.

## Always-active features

These features have no toggle — they're always on:

- **Scribe** (notes, learnings, handoffs) — `/note`, `/notes`
- **Session startup** — context injected automatically via `UserPromptSubmit` hook (toggled by `auto-startup` flag); `/backfill-handoffs` processes unseen transcripts
- **Install checker** — verifies installed files match repo on first tool call
- **Transcript saver** — saves session transcript on session end
