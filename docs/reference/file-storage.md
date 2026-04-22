---
type: reference
title: File Storage
scope: project
description: Runtime data locations — where JSONL logs, flags, transcripts, and session state live
framework_version: "1.0"
last_verified: 2026-04-22
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

## Compass data

Compass state lives at `<repo-root>/.apiary/compass/` under the umbrella `.apiary/` directory. All git-ignored.

| Path | Description |
|------|-------------|
| `compass/observations/<session_id_short>.json` | Per-session personality observations written by `/wrapup` capture or `compass/backfill.py` |
| `compass/observations/archive/<iso-year>-<iso-week>/` | Archived observations (moved when active count ≥ 50 AND age ≥ 90 days) |
| `compass/personality.md` | Synthesized personality profile, regenerated weekly. Read at startup by `/apiary-context` |
| `compass/corrections.md` | Optional manual high-weight evidence the synthesizer treats above raw observations |

The dimensions config (`compass/dimensions.json`) ships in the repo, not under `.apiary/` — it's source code, not state.

## Refiner data

| File | Path | Description |
|------|------|-------------|
| Round counter | `refiner/tmp/round_<session-id>.json` | Per-session refinement round count (repo-local, git-ignored) |

## GUI data

GUI per-instance state lives under `~/.claude/apiary_gui/` (default profile). Setting `APIARY_GUI_PROFILE=<name>` re-roots everything to `~/.claude/apiary_gui_<name>/`, isolating a "dev" build from the main one. Path resolution is centralized in `gui/paths.py` (`state_dir()`, `mutex_name()`, `window_title()`).

| File | Path | Description |
|------|------|-------------|
| Tab state | `~/.claude/apiary_gui/tabs.json` | Open tab cwds + active index (restored on relaunch) |
| Sidebar state | `~/.claude/apiary_gui/sidebar_state.json` | Per-group collapsed/expanded state |
| Composer state | `~/.claude/apiary_gui/composer_state.json` | Chat input height in pixels (set by dragging the gutter above the input) |
| Theme | `~/.claude/apiary_gui/theme.json` | CSS variable values (hot-reloads via watchdog) |
| Launch config | `~/.claude/apiary_gui/launch.json` | Claude Code spawn args + cwd |
| Captures | `~/.claude/apiary_gui/captures/<ts>-<label>.bin` | Raw pty-output captures (binary), populated only when `APIARY_GUI_CAPTURE_LABEL` is set |
| Permission-MCP config | `~/.claude/apiary_gui/permission_mcp_config.json` | `--mcp-config` file handed to claude when `APIARY_PERMISSION_MCP=1` (points at `gui/permission_mcp.py`). Rewritten each session start |
| Permission-MCP log | `~/.claude/apiary_gui/permission_mcp.log` | Append-only log of START/REQUEST/DECISION/EXIT events from the permission-prompt MCP stdio server |

`theme.json`, `launch.json`, and the directory itself are auto-created on first run.

GUI build outputs (PyInstaller) live at the repo root, both git-ignored:

| Path | Description |
|------|-------------|
| `dist/apiary-gui/apiary-gui.exe` | One-folder packaged exe |
| `dist/apiary-gui/_internal/` | Bundled Python runtime, gui/web assets, dependencies |
| `build/` | PyInstaller intermediate work directory |

## Researcher data

Researcher state lives at `<repo-root>/.apiary/research/` under the umbrella `.apiary/` directory. All git-ignored.

| Path | Description |
|------|-------------|
| `research/tags.yaml` | Controlled-tag vocabulary for this repo. Edited via `researcher/cli.py register-tag` |
| `research/<topic>/<slug>.md` | One research entry per file. YAML-subset frontmatter (title, topic, tags, date_created, date_last_verified, sources) followed by Summary / Context / Findings / Code / Caveats sections |

The template (`researcher/template.md`) ships in the repo, not under `.apiary/` — it's source, not state.

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
