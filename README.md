# claude-apiary

A unified ecosystem of tools that extend and optimize the [Claude Code](https://claude.ai/claude-code) experience. Each tool is a specialized worker — together they form a hive.

---

## Install & update

One command for each, from inside the clone. They handle Python discovery, the
Windows Store python-alias trap, Poetry, the bootstrap chain, and (with the GUI
flag) the desktop app — so there's no manual multi-step recovery.

| | Windows (PowerShell) | macOS / Linux |
|---|---|---|
| **First install** | `.\scripts\install.ps1` | `./scripts/install.sh` |
| **+ desktop GUI** | `.\scripts\install.ps1 -Gui` | `./scripts/install.sh --gui` |
| **Update** | `.\scripts\update.ps1 [-Gui]` | `./scripts/update.sh [--gui]` |

Full prerequisites, the desktop GUI, and troubleshooting live in [`SETUP.md`](SETUP.md).

---

## Requirements

- **Python >= 3.11** (declared minimum supported version)
- **Standard library only** by default; the only third-party dependency is `pytest` (for the test suites). All deps are pinned to version ranges in `requirements.txt`.
- See [`docs/standards/code-style.md`](docs/standards/code-style.md) for the stdlib-only rule and [`PORTABILITY.md`](PORTABILITY.md) for prereqs, bootstrap, state locations, and portability rules.

---

## Tools

### Budgeter

Token usage monitoring and cost estimation for Claude Code sessions.

Claude Code has no built-in visibility into how many tokens a session consumes or when a task is about to get expensive. Budgeter adds that visibility by hooking into Claude Code's tool lifecycle — silently, without spending any tokens of its own.

**What it does:**
- **Logs** token consumption per tool call to a local JSONL file
- **Warns** Claude before running a response expected to be expensive, using rule-based scope detection — Claude asks you whether to proceed
- **Chains** continuation turns (mid-task clarifying questions) back to the originating request, so costs are attributed correctly by task rather than by individual tool call
- **Reports** usage history on demand via `report.py`

**How warnings work:**
When warnings are enabled and enough task history exists, the pre-hook evaluates the current assistant message against a set of scope detection rules (keyword categories, file counts, step counts). Each rule has a configurable weight; if the weighted score exceeds a threshold, the hook finds similar past tasks and injects a warning with their median cost — Claude then asks you before proceeding.

**Session-length nudge:**
Separate from cost warnings, the budgeter can inject a one-shot advisory when the current prompt size crosses configured thresholds (`session_warn_soft_tokens` / `session_warn_hard_tokens`) — suggesting Claude wrap up at a natural checkpoint and prompt you to start a fresh session. Skipped in detached runner runs. Gated independently via `/budgeter-session-warn` so you can enable it without re-enabling the cost-warning rules.

**Toggle features:**
```
/budgeter-log            # turn token logging on/off
/budgeter-warn           # turn cost estimation warnings on/off
/budgeter-session-warn   # turn session-length wrap-up nudge on/off
```

---

### Scribe

Structured note management for cross-session continuity.

Claude Code sessions are isolated — each one starts fresh with no memory of what happened before. Scribe bridges that gap with project-scoped notes that persist across sessions and are loaded automatically at startup.

**What it does:**
- **Notes** — typed operational state (TODOs, handoffs, blockers, decisions, wishlists, context) stored under the per-target state dir at `<state-dir>/scribe/` (the registry-allocated folder under `<apiary>/.repos/<name>-<id>/`)
- **Learnings** — project-specific knowledge Claude discovers during task execution (workarounds, better approaches, platform quirks). Stored separately in `learnings/`, no auto-archive
- **Handoffs** — structured session summaries generated automatically on startup from the previous session's transcript, so the next session knows what happened
- **Auto-archive** — notes older than 30 days are moved to an archive folder. Learnings persist indefinitely

**Commands:**
```
/note <text>      # save a note (type auto-detected from prefix, e.g. "TODO: ...")
/note done <N>    # mark note N as done
/notes            # list active notes
/notes search X   # search notes
/startup          # session init — generates handoff + loads notes and learnings
```

**Storage:** `<state-dir>/scribe/` — typed-year folder layout (`todos/2026/`, `handoffs/2026/`, …, each with `index.jsonl` and per-note `<seq>.md` files). Per-target state lives under `<apiary>/.repos/<name>-<id>/`, resolved automatically by the launcher. Set `APIARY_STATE_LAYOUT=legacy` only as an escape hatch to read the pre-migration `~/.claude/projects/<project-key>/` path. See [`PORTABILITY.md`](PORTABILITY.md) for the full state map.

---

### Refiner

Adversarial spec-writing tool that turns fuzzy ideas into structured handoff documents.

**Command:**
```
/refine <idea>    # start refining an idea into a handoff spec
```

---

### Harden

Adversarial code hardening loop that stress-tests code or plans.

An automated attack-defend loop where Attacker agents find weaknesses (edge cases, vulnerabilities, design flaws) and a Defender agent fixes them. Iterates until the code or plan is hardened, producing a paper trail of what was found, fixed, and deferred.

**What it does:**
- **Multi-lens attackers** (code mode default) — one read-only specialist per lens from a 7-lens taxonomy (`correctness, security, robustness, resilience, complexity, architecture, testing`), run in parallel for broad coverage
- **Consolidator/referee** dedups overlapping findings across lenses, then adjudicates accept/reject with a default-accept posture, so the Defender acts on a clean, deduped set instead of raw multi-lens noise
- **Defender** addresses each accepted finding: fixes, refactors, or defers with justification
- **Validators** ensure structured output — required fields, valid enums, file existence, referee coverage
- **IDs assigned by Python** (`ATK-SEC-001` → `CON-001` → `DEF-001`) — deterministic, not LLM-generated
- **Single-lens / legacy bypass** — one lens (or `--focus`) skips the referee and runs the original single-attacker→defender flow
- Works on code files (with worktree isolation) or scribe plan notes

**Commands:**
```
/harden file1.py file2.py                     # all 7 lenses → referee → defender
/harden app.py --lenses security,correctness  # only these lenses
/harden util.py --lenses complexity           # single lens, referee skipped
/harden --plan 79                             # harden a scribe plan/spec note (legacy path)
/harden app.py --focus security --deep        # legacy single-attacker path
```

---

### Compass

Captures personality and behavior signals across sessions and synthesizes them into a profile that future sessions read at startup, so Claude can anticipate this user's preferences — verbosity, when to ask vs decide, pushback style — and act in alignment in headless/runner sessions where it can't pause to ask.

Two-tier storage: per-session JSON observations under `<state-dir>/compass/observations/` (written inline by `/wrapup`'s Step 4 capture, or by `compass/backfill.py` for historical transcripts), and a synthesized `personality.md` rewritten weekly from those observations + `corrections.md` (manual high-weight evidence). The startup `/apiary-context` skill reads `personality.md` and uses it as soft guidance — explicit auto-memory feedback still overrides it.

```
/compass-sync                                                                            # manually re-run synthesis
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/observations.py count      # how many active observations
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/backfill.py --last 5       # backfill 5 recent transcripts
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" compass/observations.py archive    # dry-run archive sweep
```

Bloat handling: rolling archive at 50+ active observations and 90+ days old; never archives below 50. Synthesizer self-throttles to 7-day cadence (cron runs daily, no-ops 6 of 7 days). Dimensions are configured at `compass/dimensions.json`. See [`compass/CLAUDE.md`](compass/CLAUDE.md) for lane discipline (compass vs auto-memory) and observation quality bar.

---

### Researcher

Per-repo compendium of structured research findings — a place to capture what you learned from a WebSearch so next time you (or Claude) need the same answer it comes from the compendium, not another round of googling. Entries are markdown files under `<state-dir>/research/<topic>/<slug>.md` with a YAML-subset frontmatter (title, topic, tags, dates, sources) and standard sections (Summary, Context, Findings, Code, Caveats). Tags are drawn from a controlled vocabulary at `<state-dir>/research/tags.yaml`.

The `/research` skill is expected to be invoked **before** `WebSearch` on any topic plausibly in the compendium — `/research find <keywords>` first, fall through to web only on miss.

```
/research register-tag multiplayer                                        # grow the vocab
/research add unreal "Replication basics" --tags multiplayer,networking   # scaffold entry
/research find replication                                                # search compendium
/research list --topic unreal                                             # browse a topic
/research verify unreal replication-basics                                # bump last-verified date
```

---

### Runner

Autonomous six-stage orchestrator that takes a backlog ticket from fuzzy idea to review-ready code without a human in the loop. Designed to run overnight via cron.

Where the other tools extend an in-session Claude Code loop, Runner *replaces* that loop — it composes Refiner, the executor, Harden, and Budgeter into a hands-off build system. You pick a ticket, it runs, and you review the resulting branch the next morning.

**The six stages** (each a separate script consuming the previous stage's JSON artifact):

1. **validate_intake** — schema-checks the intake file
2. **auto_refine** — turns the intake into a structured spec via the refiner
3. **auto_plan** — turns the spec into a step-by-step execution plan
4. **executor** — creates a feature branch (`runner/<uuid>`) and makes the code changes, committing per step
5. **auto_harden** — runs the attack-defend loop on the resulting branch
6. **approval** — final gate; auto-merges clean runs or flags for review

**Artifacts** flow through per-stage directories keyed by UUID: `intake/ → specs/ → plans/ → executions/ → hardens/ → reports/`.

**Ticket lifecycle:**
```
python -m runner.draft_ticket --title "..." --problem "..." --scope "..."   # creates backlog JSON
python -m runner.promote <slug>                                              # backlog → ready (intake)
python -m runner.run runner/intake/<uuid>.json                               # runs all 6 stages
python -m runner.mark_done <slug>                                            # close a ticket hand-fixed outside the runner
```

**Cost accounting:** every stage's `<usage>` XML is piped to `budgeter/log_agent_cost.py` with the run UUID as both `session_id` and `request_id`, so `budgeter/report.py --by-request` sums the entire run as one line.

**Safety:** `NO_USAGE_STAGES` whitelists stages that legitimately make no Claude calls; any other zero-usage stage aborts the run so token caps can't be bypassed. Each handoff is gated by a validator (`validate_intake.py`, `validate_spec.py`, `validate_plan.py`).

**Overnight / detached mode:** `detached_lib.py` handles branch creation, claim-based backlog picking, hygiene prechecks, and appends to an overnight log. Cron setup is in [`runner/scheduling.md`](runner/scheduling.md). To detect and fix drift in the registered scheduled task (after repo moves, renames, or bootstrap runs), use `python -m runner.cron_health check` or `... repair --apply`; the canonical state lives in `runner/cron_registry.json`.

---

### GUI

Native Windows desktop wrapper around Claude Code. **Windows-only V1; in active development, not yet polished for distribution.** Spec: scribe note `C-2026-32`. Stdlib-only deviation: `D-2026-47`.

Working in Claude Code's terminal UI is painful for visibility and ergonomics: scrollback is cluttered with tool calls and system reminders, there's no per-message timestamps or token counts, and styling is fixed. The GUI spawns Claude Code as a hidden pty subprocess and presents a clean chat view, a global scribe sidebar, and a small terminal strip for interactive prompts.

**What it does:**
- **Filtered chat** — renders only user-authored prompts and assistant text from the session JSONL; drops tool_use, tool_result, system reminders, and hook context
- **Per-message + cumulative token counts** in the header
- **Multi-tab / multi-cwd** — each tab owns its own claude pty, transcript tail, and scribe sidebar scoped to that cwd
- **Global scribe sidebar** — searchable per-type note groups for the active tab's repo
- **Hot-reloadable theme** — edit `<main-apiary>/.apiary/gui/apiary_gui/theme.json`, watcher applies live
- **PyInstaller one-folder build** for a single double-clickable `.exe` (with three-hex taskbar icon)

**Run from source:**
```bash
poetry install --with gui
poetry run python -m gui.app
```

**Build the `.exe`:**
```bash
poetry run pip install "pyinstaller>=6.0,<7.0"
poetry run python gui/packaging/build.py
# → dist/apiary-gui/apiary-gui.exe
```

**Working on the GUI from inside the GUI:**
The GUI is single-instance per profile. Set `APIARY_GUI_PROFILE=dev` to run a second instance with isolated state (`<main-apiary>/.apiary/gui/apiary_gui_dev/`) alongside your main one. `Ctrl+R` / `F5` reloads the frontend without restarting the Python backend.

Full details: [`gui/README.md`](gui/README.md).

---

## Repository Structure

```
claude-apiary/
├── core/                        # Shared infrastructure used by all tools
│   ├── flags.py                 # Flag file management (~/.claude/{name}-enabled)
│   ├── config.py                # JSON config loader with defaults fallback
│   ├── hooks_lib.py             # Hook registration and settings.json management
│   └── hooks/
│       ├── save_transcript.py   # Stop hook: saves stripped session transcript
│       ├── check_install.py     # PreToolUse hook: verifies installed files match repo
│       └── check_install_stop.py # Stop hook: cleanup for install checker
│
├── budgeter/                    # Token usage monitoring tool
│   ├── hooks/
│   │   ├── pre_tool_use.py      # Main hook: logs previous tool cost, warns if expensive
│   │   ├── post_tool_use.py     # Logs Agent token cost from tool_response.totalTokens
│   │   └── stop_session.py      # Logs final tool cost, cleans up temp files
│   ├── lib/
│   │   ├── logger.py            # All file I/O: log, baseline, snapshot, session JSONL
│   │   └── estimator.py         # Rule-based scope detection + percentile threshold logic
│   ├── commands/
│   │   ├── budgeter-log.md           # /budgeter-log slash command definition
│   │   ├── budgeter-warn.md          # /budgeter-warn slash command definition
│   │   ├── budgeter-session-warn.md  # /budgeter-session-warn slash command definition
│   │   └── budgeter-setup.md         # /budgeter-setup slash command definition
│   ├── data/                    # Runtime — usage_log.jsonl (git-ignored)
│   ├── tmp/                     # Runtime — per-session baseline files (git-ignored)
│   ├── config.json              # Default global config
│   ├── report.py                # CLI report tool
│   └── test_hooks.py            # Unit + integration tests
│
├── scribe/                      # Structured note management for session continuity
│   ├── commands/
│   │   ├── note.md              # /note command — add a typed note
│   │   └── notes.md             # /notes command — query and list notes
│   ├── notes.py                 # Core CLI: notes, learnings, handoffs, archive
│   └── test_notes.py            # Tests for notes.py
│
├── refiner/                     # Idea-to-spec refinement tool
│   ├── commands/
│   │   └── refine.md            # /refine slash command definition
│   └── round_counter.py         # Round tracking for refinement loops
│
├── harden/                      # Adversarial code hardening tool
│   ├── agents/
│   │   ├── attacker.md          # Legacy single-attacker prompt template
│   │   ├── attacker_lens.md     # Per-lens specialist attacker prompt (multi-lens path)
│   │   ├── consolidator.md      # Referee: dedup + default-accept adjudication
│   │   └── defender.md          # Defender agent prompt template
│   ├── commands/
│   │   └── harden.md            # /harden slash command definition
│   ├── lenses.py                # 7-lens taxonomy: names, codes, briefs, seam rules
│   ├── assign_ids.py            # Post-processor: assigns ATK / ATK-<CODE> / CON / DEF IDs
│   ├── validate_findings.py     # Validates Attacker output (legacy + lens mode)
│   ├── validate_consolidation.py # Validates Consolidator output + degrade fallback
│   ├── validate_response.py     # Validates Defender output structure
│   ├── validate_and_assign.py   # Combined validate + assign-IDs step (findings/response/consolidation)
│   ├── round_counter.py         # Round tracking for harden loops
│   └── tmp/                     # Runtime — round state files (git-ignored)
│
├── compass/                     # Personality profile and behavioral read
│   ├── commands/
│   │   └── compass-sync.md      # /compass-sync slash command definition
│   ├── store.py                 # Path resolution, dimension config, validation helpers
│   ├── observations.py          # CLI: count, list, validate, archive observation files
│   ├── synthesize.py            # CLI: read observations → headless claude → personality.md
│   ├── backfill.py              # CLI: extract observations from historical transcripts
│   ├── dimensions.json          # Configured personality dimensions
│   ├── CLAUDE.md                # Lane discipline, observation quality bar, synthesis cycle
│   └── test_store.py            # Tests
│
├── researcher/                  # Per-repo compendium of structured research findings
│   ├── commands/
│   │   └── research.md          # /research slash command definition
│   ├── cli.py                   # CLI: add, find, list, show, verify, register-tag
│   ├── store.py                 # Path resolution, template, frontmatter I/O
│   ├── _yaml_mini.py            # Minimal YAML subset parser (stdlib-only)
│   ├── template.md              # Entry body template (Summary / Context / Findings / …)
│   └── test_researcher.py       # Tests
│
├── runner/                      # Autonomous 6-stage orchestrator
│   ├── commands/
│   │   └── runner-prep.md       # /runner-prep slash command definition
│   ├── run.py                   # End-to-end orchestrator (all 6 stages)
│   ├── validate_intake.py       # Stage 1: schema-check intake JSON
│   ├── auto_refine.py           # Stage 2: intake → spec
│   ├── validate_spec.py         # Spec schema gate
│   ├── auto_plan.py             # Stage 3: spec → plan
│   ├── validate_plan.py         # Plan schema gate
│   ├── executor.py              # Stage 4: plan → code changes on runner/<uuid> branch
│   ├── auto_harden.py           # Stage 5: attack-defend loop on the resulting branch
│   ├── approval.py              # Stage 6: auto-merge or flag for review
│   ├── create_intake.py         # Create an intake JSON directly
│   ├── draft_ticket.py          # Create a backlog draft ticket
│   ├── promote.py               # Promote a backlog draft to intake
│   ├── mark_done.py             # Close a ticket hand-fixed outside the runner
│   ├── queue.py                 # Backlog queue helpers
│   ├── detached_lib.py          # Overnight/cron detached-mode helpers
│   ├── cron_health.py           # Check/repair OS scheduler against cron_registry.json
│   ├── cron_registry.json       # Canonical scheduled-entry registry
│   ├── schedulers/              # Backend protocol + Windows Task Scheduler impl
│   ├── claude_subprocess.py     # Claude CLI wrapper shared by stages
│   ├── cost_emit.py             # Emits <usage> XML from Claude envelope
│   ├── config_loader.py         # Shared config loader
│   ├── config.json              # Default orchestrator/stage config
│   ├── scheduling.md            # Cron/schedule setup (supersedes cron_setup.md)
│   ├── backlog/                 # Draft tickets (ticketed but not yet run)
│   ├── intake/                  # Stage 1 input JSON files (<uuid>.json)
│   ├── specs/                   # Stage 2 output (git-ignored)
│   ├── plans/                   # Stage 3 output (git-ignored)
│   ├── executions/              # Stage 4 output (git-ignored)
│   ├── hardens/                 # Stage 5 output (git-ignored)
│   └── reports/                 # Stage 6 output (git-ignored)
│
├── setup.py                     # Unified installer for all tools
├── SETUP.md                     # Setup instructions
└── .gitignore
```

---

## How It All Comes Together

For detailed architecture documentation, see [`docs/architecture/`](docs/architecture/):

- **[System Overview](docs/architecture/system-overview.md)** — component map, data flow, shared core, and design rationale
- **[Hook Lifecycle](docs/architecture/hook-lifecycle.md)** — PRE-to-PRE delta pattern, agent special case, CONT task chaining, scope detection

Additional reference documentation lives in [`docs/`](docs/_index.md):

- **[CLI Tools](docs/reference/cli-tools.md)** — all Python entry points with subcommands and flags
- **[Slash Commands](docs/reference/slash-commands.md)** — all commands and when to use them
- **[Hooks](docs/reference/hooks.md)** — registered hooks and execution order
- **[Config Files](docs/reference/config-files.md)** — configuration and state files
- **[File Storage](docs/reference/file-storage.md)** — where runtime data lives
- **[Code Style](docs/standards/code-style.md)** — coding conventions for this project
- **[New Tool Checklist](docs/standards/new-tool-checklist.md)** — what a new tool needs

---

## Configuration

Edit `budgeter/config.json` (global) or `.claude/budgeter.json` (per-project):

| Field | Default | Description |
|---|---|---|
| `monitored_tools` | `["Agent", "Bash", "Read", "Write"]` | Tool types to track |
| `min_tasks` | `50` | Minimum unique tasks logged before warnings activate |
| `expensive_token_threshold` | `null` | Hard token limit. If set, overrides percentile |
| `expensive_percentile` | `90` | Percentile of historical task costs used as warning threshold |
| `similarity_top_n` | `10` | Number of similar past tasks to compare against |

---

## Reporting

```bash
python budgeter/report.py                    # grouped by session (default)
python budgeter/report.py --by-turn          # grouped by session > task
python budgeter/report.py --flat             # flat chronological list
python budgeter/report.py --all              # include zero-delta entries
python budgeter/report.py --date 2026-03-14  # single date
python budgeter/report.py --since 2026-03-01 # from date onwards
```

---

## Testing

```bash
python budgeter/test_hooks.py      # unit + integration tests for budgeter
```
