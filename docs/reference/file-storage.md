---
type: reference
title: File Storage
scope: project
description: Runtime data locations — where JSONL logs, flags, transcripts, and session state live
framework_version: "1.0"
last_verified: 2026-04-07
---

# File Storage

All runtime data is stored outside the repo under `~/.claude/` (git-ignored), with a few exceptions for repo-local state.

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

All scribe data is project-scoped under `~/.claude/projects/<project-key>/`:

| File | Description |
|------|-------------|
| `notes.jsonl` | Active notes (TODOs, handoffs, decisions, etc.) |
| `learnings.jsonl` | Project-specific learnings (no auto-archive) |
| `notes_archive.jsonl` | Archived notes (older than 30 days) |

The `<project-key>` is derived from the absolute path of the working directory (e.g. `D--Professional-claude-apiary`).

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
