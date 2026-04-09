---
type: reference
title: File Storage
scope: project
description: Runtime data locations — where JSONL logs, flags, transcripts, and session state live
framework_version: "1.0"
last_verified: 2026-04-09
---

# File Storage

Runtime data splits across two locations: user-global state under `~/.claude/` (git-ignored, shared across all repos) and repo-local state under `<repo-root>/.apiary/` (self-ignored via `.apiary/.gitignore`, travels with the checkout). Scribe state lives in the repo-local umbrella; budgeter logs, transcripts, and Claude Code's own session files stay under `~/.claude/`.

## Runner data

All runner artifacts are repo-local under `runner/`:

| Directory | Description |
|-----------|-------------|
| `runner/intake/` | Intake JSON files (`<uuid>.json`) — runner input |
| `runner/specs/` | Refined spec JSON files (`<uuid>.json`) — stage 2 output |
| `runner/plans/` | Plan JSON files (`<uuid>.json`) — stage 3 output |
| `runner/executions/` | Execution log JSON files (`<uuid>.json`) — stage 4 output |
| `runner/hardens/` | Harden result JSON files (`<uuid>.json`) — stage 5 output |
| `runner/reports/` | Approval report JSON files (`<uuid>.json`) — stage 6 output |

Each runner run is keyed by a UUID generated at intake creation. All artifact directories are git-ignored except `runner/intake/`.

## Flag files

Toggle state stored as sentinel files. Presence = enabled, absence = disabled.

| Flag | Path | Set by |
|------|------|--------|
| Budgeter logging | `~/.claude/budgeter-log-enabled` | `/budgeter-log` |
| Budgeter warnings | `~/.claude/budgeter-warn-enabled` | `/budgeter-warn` |

Managed via `core/flags.py`: `flags.is_enabled("budgeter-log")`, `flags.enable(name)`, `flags.disable(name)`.

## Budgeter data

| File | Path | Description |
|------|------|-------------|
| Usage log | `budgeter/data/usage_log.jsonl` | All token usage entries (repo-local, git-ignored) |
| Feedback log | `budgeter/data/feedback.jsonl` | User feedback on warning accuracy |
| Baseline files | `budgeter/tmp/baseline_<session>.json` | Per-session token baselines for PRE-to-PRE delta (cleaned up on session end) |

## Scribe data

All scribe data lives inside the repo checkout at `<repo-root>/.apiary/scribe/`, under the umbrella `<repo-root>/.apiary/` state directory. The umbrella self-ignores via `.apiary/.gitignore` (contents: `*`), so nothing inside it is tracked by git.

| File | Description |
|------|-------------|
| `notes.jsonl` | Active notes (TODOs, handoffs, decisions, etc.) |
| `notes_archive.jsonl` | Archived notes (older than 30 days) |
| `learnings.jsonl` | Project-specific learnings (no auto-archive) |
| `backfill_skip.json` | Session IDs skipped from unseen-session detection |
| `memory/` | Permanent memory files, indexed via `MEMORY.md` |

**Layout gate.** Scribe's path resolution is selected by the `APIARY_STATE_LAYOUT` environment variable. When set to `repo`, the helpers in `scribe.notes` run `git rev-parse --show-toplevel` on the session's cwd and resolve state under `<repo-root>/.apiary/scribe/`. When unset (current default), they fall back to the legacy per-project location `~/.claude/projects/<project-key>/{notes,notes_archive,learnings}.jsonl` where `<project-key>` comes from the `.claude-project-key` marker file. Todo #268 flips the default; decision #269 tracks the migration.

**Historical note.** Before decision #269, scribe state was project-scoped under `~/.claude/projects/<project-key>/`. The `<project-key>` was originally derived from the absolute cwd path (e.g. `D--Professional-claude-apiary`); the T5c migration moved it to a stable key read from the `.claude-project-key` marker file. The current in-repo layout supersedes both schemes.

## Refiner data

| File | Path | Description |
|------|------|-------------|
| Round counter | `refiner/tmp/round_<session-id>.json` | Per-session refinement round count (repo-local, git-ignored) |

## Transcripts

| File | Path | Description |
|------|------|-------------|
| Last transcript | `~/.claude/.last-transcript.jsonl` | Most recent session transcript (for handoff generation) |
| Transcript archive | `~/.claude/transcripts/` | Saved transcripts by session ID |

## Session state (repo-local)

| File | Path | Description |
|------|------|-------------|
| Session identity | `.claude-session-identity.json` | Current session ID, role, mission (git-ignored) |

## Installed files (in ~/.claude/)

These are copied from the repo by `setup.py --global`:

| Source | Destination |
|--------|-------------|
| `*/commands/*.md` | `~/.claude/commands/<name>.md` |
