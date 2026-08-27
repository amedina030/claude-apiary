---
type: reference
title: Slash Commands
scope: project
description: All slash commands, what they do, and when to use them
framework_version: "1.0"
last_verified: 2026-08-26
---

# Slash Commands

Slash commands are defined in markdown files under `commands/` directories. Claude Code loads them at session start from `~/.claude/commands/`. They are invoked by typing `/<name>` in the Claude Code prompt.

## Command list

| Command | Source | Description |
|---------|--------|-------------|
| `/apiary-context` | `core/commands/apiary-context.md` | Load apiary toolkit context (scribe, budgeter, runner, portability rules) |
| `/budgeter` | `budgeter/commands/budgeter.md` | Toggle one budgeter feature on/off: `log`, `warn`, or `session-warn` |
| `/budgeter-setup` | `budgeter/commands/budgeter-setup.md` | Set up budgeter for a specific project |
| `/note` | `scribe/commands/note.md` | Add a typed note (type auto-detected from prefix) |
| `/notes` | `scribe/commands/notes.md` | List and query notes |
| `/review-learnings` | `scribe/commands/review-learnings.md` | Walk through all learnings grouped by tag, archive or supersede stale entries, stamp `last_review` timestamp |
| `/refine` | `refiner/commands/refine.md` | Refine a fuzzy idea into a structured handoff spec through value-first adversarial questioning |
| `/harden` | `harden/commands/harden.md` | Adversarial attack-defend loop that stress-tests code or plans |
| `/review` | `docs/commands/review.md` | Review changes against standards, then fix issues |
| `/wrapup` | `core/commands/wrapup.md` | Commit, capture learnings + TODOs, and generate a session handoff note. Also captures compass observations from the session (Step 4, non-blocking). |
| `/compass-sync` | `compass/commands/compass-sync.md` | Manually trigger compass synthesis — regenerate `personality.md` from active observations |
| `/research` | `researcher/commands/research.md` | Add, find, list, show, verify, or register tags for research findings stored per-target under `<state-dir>/research/` |
| `/runner-prep` | `runner/commands/runner-prep.md` | Audit active scribe todos and prepare automation-ready intake JSONs for the runner pipeline, with dependency ordering and safety classification |
| `/incubator` | `incubator/commands/incubator.md` | Spawn a new side-project repo wired up with the apiary toolkit — refines the idea, creates a git repo, drops a Python+poetry skeleton, migrates the spec into the new repo's scribe |

## Toggles

`/budgeter <log|session-warn>` creates or removes a sentinel file at
`<repo>/.claude/apiary/flags/<flag-name>-enabled`. Presence = enabled.

| Invocation | Flag file | Default |
|------------|-----------|---------|
| `/budgeter log` | `<repo>/.claude/apiary/flags/budgeter-log-enabled` | off |
| `/budgeter session-warn` | `<repo>/.claude/apiary/flags/budgeter-session-warn-enabled` | off |

Toggles are per-repo and persist across sessions. The skill shells out to
`core/flags.py` (see [CLI Tools](cli-tools.md#coreflagspy)), which is the same
code path the hooks read — so what the toggle writes is what they check.

## Always-active features

These features have no toggle — they're always on:

- **Scribe** (notes, learnings, handoffs) — `/note`, `/notes`
- **Session startup** — context injected automatically via `UserPromptSubmit` hook (toggled by `auto-startup` flag)
- **Install checker** — verifies installed files match repo on first tool call
- **Transcript saver** — saves session transcript on session end
