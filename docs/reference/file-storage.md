---
type: reference
title: File Storage
scope: project
description: Runtime data locations — where JSONL logs, flags, transcripts, and session state live
framework_version: "1.0"
last_verified: 2026-05-04
---

# File Storage

Runtime data splits across three locations:

- **Per-target state** (post C-2026-46) lives under `<apiary-repo>/.repos/<name>-<id>/`, where `<apiary-repo>` is wherever the apiary toolkit is checked out and `<name>-<id>` is the registry-allocated folder for a given target repo. The launcher resolves the path via `git rev-parse --show-toplevel` on the session's cwd, looks the repo up in `<apiary-repo>/.repos/registry.json`, allocates an id on first call, and exports `APIARY_TARGET_STATE_DIR=<apiary-repo>/.repos/<name>-<id>` for the child process. Tools that respect this env var (scribe, captures, researcher, runner, compass, apiary_bootstrap) read/write under it. A breadcrumb pointer at `<target>/.apiary/pointer` lets passive consumers (GUI) reverse-resolve without rerunning the registry.
- **User-global state** under `~/.claude/` — shared across all repos (transcripts, GUI per-instance state, flags).
- **Apiary repo–local files** that aren't state — source, profiles, dimension configs.

Source of truth for the centralization spec: scribe note `C-2026-46`; resolver lives in `core/utils/state.py`.

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

Scribe state lives at `<state-dir>/scribe/` under the per-target dir resolved by the registry (see intro). Folder-per-type layout, with each note's body as `<id>.md` and a sibling `index.jsonl` for fast listing.

| Path | Description |
|------|-------------|
| `scribe/<type>/<year>/<seq>.md` | Note body. `<type>` is `todos`, `handoffs`, `decisions`, `wishlists`, `blockers`, `references`, `context`, `general`, or `learnings` |
| `scribe/<type>/<year>/index.jsonl` | One line per note for fast listing |
| `scribe/<type>/<year>/next_seq` | Monotonic per-(type, year) sequence counter |
| `scribe/<type>/archive/<year>/` | Archived bodies + index (auto-archived after 30 days; learnings never auto-archive) |
| `scribe/memory/MEMORY.md` | One-line index of permanent memory entries; loaded into startup context |
| `scribe/memory/<topic>.md` | One memory entry per file, kebab-case slug |

**Path resolution.** `scribe.notes.scribe_state_dir()` returns `$APIARY_TARGET_STATE_DIR/scribe` when the launcher set the env var, otherwise falls back to `<git-repo-root>/.apiary/scribe/` for unmigrated targets. `APIARY_STATE_LAYOUT=legacy` is an escape hatch that drops back to the historical `~/.claude/projects/<project-key>/` location.

## Compass data

Compass state lives at `<state-dir>/compass/`. All paths are git-ignored (the apiary repo's `.gitignore` excludes `.repos/`).

| Path | Description |
|------|-------------|
| `compass/observations/<session_id_short>.json` | Per-session personality observations written by `/wrapup` capture or `compass/backfill.py` |
| `compass/observations/archive/<iso-year>-<iso-week>/` | Archived observations (moved when active count ≥ 50 AND age ≥ 90 days) |
| `compass/personality.md` | Synthesized personality profile, regenerated weekly. Read at startup by `/apiary-context` |
| `compass/corrections.md` | Optional manual high-weight evidence the synthesizer treats above raw observations |

The dimensions config (`compass/dimensions.json`) ships in the apiary repo, not under per-target state — it's source code, not state.

## Refiner data

| File | Path | Description |
|------|------|-------------|
| Round counter | `refiner/tmp/round_<session-id>.json` | Per-session refinement round count (repo-local, git-ignored) |

## GUI data

GUI per-instance state lives under `<main-apiary>/.apiary/gui/apiary_gui/` (default profile). Setting `APIARY_GUI_PROFILE=<name>` re-roots everything to `<main-apiary>/.apiary/gui/apiary_gui_<name>/`, isolating a "dev" build from the main one. Path resolution is centralized in `gui/paths.py` (`state_dir()`, `mutex_name()`, `window_title()`). Resolving `<main-apiary>` depends on the build: a **source** build uses `gui/paths.py`'s grandparent (the checkout root); a **frozen** PyInstaller build can't trust `__file__` (it points inside `_internal/`, which is wiped on every rebuild), so it walks up from the exe (`sys.executable`) to find the apiary checkout the build sits in — the first ancestor containing both `.git` and `gui/`. If the build was shipped outside any checkout, it falls back to a per-user data dir (`%LOCALAPPDATA%` on Windows, `Application Support` on macOS, `$XDG_DATA_HOME` elsewhere) so state still survives rebuilds.

| File | Path | Description |
|------|------|-------------|
| Tab state | `<main-apiary>/.apiary/gui/apiary_gui/tabs.json` | Open tab cwds + active index (restored on relaunch) |
| Sidebar state | `<main-apiary>/.apiary/gui/apiary_gui/sidebar_state.json` | Per-group collapsed/expanded state |
| Composer state | `<main-apiary>/.apiary/gui/apiary_gui/composer_state.json` | Chat input height in pixels (set by dragging the gutter above the input) |
| Theme | `<main-apiary>/.apiary/gui/apiary_gui/theme.json` | CSS variable values (hot-reloads via watchdog) |
| Launch config | `<main-apiary>/.apiary/gui/apiary_gui/launch.json` | Claude Code spawn args + cwd + feature flags (schema in `docs/reference/config-files.md`) |
| Captures | `<main-apiary>/.apiary/gui/apiary_gui/captures/<ts>-<label>.bin` | Raw pty-output captures (binary), populated only when `APIARY_GUI_CAPTURE_LABEL` is set |
| Drag-drop / paste file references | `<main-apiary>/.apiary/gui/apiary_gui/file_refs/<session_id>.json` | JSON list of files staged for the composer — path, name, type, size, added-timestamp, `owned` (`gui/file_refs.py`). **Scoped per tab**: each GUI tab (`Session.session_id`) gets its own file so staged files belong to the tab they were added in and never attach to another tab's message. Dragged files are references (not copies): pywebview hands the host the real dropped path. Pasted clipboard images have no source path, so their bytes are materialized into an owned temp file (see next row) and the entry is flagged `owned`. Every scope is wiped on GUI startup (`FileRefs.wipe_all`) and a tab's scope is removed when it closes (`FileRefs.destroy`); the composer appends each new path to the outgoing prompt so Claude can Read it in place |
| Pasted-image temp files | `<main-apiary>/.apiary/gui/apiary_gui/pasted/<session_id>/<id>.<ext>` | Owned copies of images pasted into a tab's composer (clipboard bitmaps have no on-disk original), under a per-tab subdir. Created by `FileRefs.add_pasted_bytes`; deleted on remove/clear, on tab close, and the whole `pasted/` tree is wiped on every GUI startup. Only `owned` entries map here — dragged references never live in this dir |
| Permission-MCP config | `<main-apiary>/.apiary/gui/apiary_gui/permission_mcp_config.json` | `--mcp-config` file handed to claude when `APIARY_PERMISSION_MCP=1` (points at `gui/permission_mcp.py`). Rewritten each session start |
| Permission-MCP log | `<main-apiary>/.apiary/gui/apiary_gui/permission_mcp.log` | Append-only log of START/REQUEST/DECISION/EXIT events from the permission-prompt MCP stdio server |

`theme.json`, `launch.json`, and the directory itself are auto-created on first run.

GUI build outputs (PyInstaller) live at the repo root, both git-ignored:

| Path | Description |
|------|-------------|
| `dist/apiary-gui/apiary-gui.exe` | One-folder packaged exe |
| `dist/apiary-gui/_internal/` | Bundled Python runtime, gui/web assets, dependencies |
| `build/` | PyInstaller intermediate work directory |

## Researcher data

Researcher state lives at `<state-dir>/research/`. All git-ignored.

| Path | Description |
|------|-------------|
| `research/tags.yaml` | Controlled-tag vocabulary for this repo. Edited via `researcher/cli.py register-tag` |
| `research/<topic>/<slug>.md` | One research entry per file. YAML-subset frontmatter (title, topic, tags, date_created, date_last_verified, sources) followed by Summary / Context / Findings / Code / Caveats sections |

The template (`researcher/template.md`) ships in the apiary repo — it's source, not state.

## Captures data

Captures state lives at `<state-dir>/captures/`. All git-ignored. Each capture is a pair of files sharing a slug.

| Path | Description |
|------|-------------|
| `captures/tags.yaml` | Controlled-tag vocabulary for this repo. Edited via `captures/cli.py register-tag` |
| `captures/<topic>/<slug>.<ext>` | The canonical image file. Extension preserved from the source (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`) |
| `captures/<topic>/<slug>.md` | Sidecar metadata. YAML-subset frontmatter (title, topic, tags, captured_at, image, optional session_id, related_notes, sources) followed by a free-text context body |

## Bootstrap state

`core/apiary_bootstrap.py` writes its provenance record at `<state-dir>/bootstrap_state.json`. For pre-migration installs that left the file at `<target>/.apiary/bootstrap_state.json`, the first centralized run reads it once as fallback, then writes only to the centralized path.

| Field | Description |
|------|-------------|
| `schema_version` | State file schema version (currently 1) |
| `profile` | Profile name applied on the last bootstrap |
| `profiles_applied` | Full extends chain in merge order (parents before children) |
| `profile_content_hashes` | `{name: "sha256:..."}` per applied profile — detects profile drift between runs |
| `applied_apiary_keys` | Top-level keys in `.claude/settings.json` that apiary owns after this bootstrap |
| `last_bootstrap_ts` | ISO-8601 UTC timestamp of the last successful run |

The state file is the signal that re-run drift-detection fires against. Absent state → bootstrap treats the target as fresh. Present state with drift → diff + prompt unless `--force`.

Profile manifests themselves live at `<apiary-repo>/profiles/<name>.jsonc` (not target-repo state). See [Bootstrapping a repo](../guides/bootstrapping-a-repo.md).

## Transcripts

| File | Path | Description |
|------|------|-------------|
| Last transcript | `~/.claude/.last-transcript.jsonl` | Most recent session transcript (for handoff generation) |
| Transcript archive | `~/.claude/transcripts/` | Saved transcripts by session ID |

## Session state (repo-local)

| File | Path | Description |
|------|------|-------------|
| Session identity | `.claude-session-identity.json` | Current session ID, role, mission (git-ignored) |

## Installed files (per bootstrapped repo)

These are written into each bootstrapped repo by `apiary install --target <repo>`:

| Source | Destination |
|--------|-------------|
| `<main-apiary>/<tool>/commands/*.md` | `<repo>/.claude/commands/<name>.md` |
| `core/launcher_template.LAUNCHER_PY` | `<repo>/.claude/apiary/launch.py` |
| (generated) | `<repo>/.claude/apiary/{main-apiary-pointer,self-pointer,version}.json` |
| Resolved profile + `core/hooks_factory` | `<repo>/.claude/settings.json` |
| `core/context_rules` rendered zone | `<repo>/CLAUDE.md` (sentinel-bounded) |
