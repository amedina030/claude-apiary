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
- **Standard library only** at runtime; the third-party dependencies are `pytest` + `pytest-cov` (test suites, `dev` group) and the optional `gui` group nothing outside `gui/` imports. All are pinned to version ranges in `pyproject.toml`, with `requirements.txt` as a pip-only fallback for the test deps.
- See [`docs/standards/code-style.md`](docs/standards/code-style.md) for the stdlib-only rule and [`PORTABILITY.md`](PORTABILITY.md) for prereqs, bootstrap, state locations, and portability rules.

---

## Tools

### Budgeter

Token usage monitoring for Claude Code sessions.

Claude Code has no built-in visibility into how many tokens a session consumes. Budgeter adds that visibility by hooking into Claude Code's tool lifecycle — silently, without spending any tokens of its own.

**What it does:**
- **Logs** token consumption per tool call to a local JSONL file, attributed to the user turn that started the task
- **Nudges** you to wrap up when the session's context gets long
- **Reports** usage history on demand via `report.py`

**How the measurement works:**
Tokens can't be read during a tool call, only between calls. Each PreToolUse hook computes the delta since the previous one and logs it against the *previous* tool; the Stop hook catches the last call of the turn. Subagents run in their own transcript, so their cost comes from the PostToolUse payload instead. See [Hook Lifecycle](docs/architecture/hook-lifecycle.md).

**Session-length nudge:**
A one-shot advisory when the current prompt size crosses configured thresholds (`session_warn_soft_tokens` / `session_warn_hard_tokens`) — suggesting Claude wrap up at a natural checkpoint and prompt you to start a fresh session. Skipped in detached runner runs. Gated by `/budgeter session-warn`.

A rule-based "this response looks expensive" warning used to sit alongside the log. Measured over 3,717 real tasks it fired at 9% precision against a 25% base rate, and it was deleted in the 2026-08 review along with its tuner, feedback log and `budgeter-warn` flag.

**Toggle features:**
```
/budgeter log            # turn token logging on/off
/budgeter session-warn   # turn session-length wrap-up nudge on/off
```
Each writes a sentinel file at `<repo>/.claude/apiary/flags/<flag-name>-enabled`;
toggles are per-repo and persist across sessions.

---

### Scribe

Structured note management for cross-session continuity.

Claude Code sessions are isolated — each one starts fresh with no memory of what happened before. Scribe bridges that gap with project-scoped notes that persist across sessions and are loaded automatically at startup.

**What it does:**
- **Notes** — typed operational state (TODOs, handoffs, blockers, decisions, wishlists, context) stored under the per-target state dir at `<state-dir>/scribe/` (the registry-allocated folder under `<apiary>/.repos/<name>-<id>/`)
- **Learnings** — project-specific knowledge Claude discovers during task execution (workarounds, better approaches, platform quirks). Stored separately in `learnings/`, no auto-archive
- **Handoffs** — structured session summaries written by `/wrapup` at the end of a session; the startup hook injects the newest one for your `(role, mission)` so the next session knows where things stopped
- **Auto-archive** — a per-type retention sweep (`scribe/policy.py`): context after 3 days, decisions after 30, anything closed 1 day after it was marked done, all but the newest handoff per `(role, mission)`. Todos, wishlists and blockers never age out, and learnings never auto-archive. The table is in [`scribe/CLAUDE.md`](scribe/CLAUDE.md)

**Commands:**
```
/note <text>      # save a note (type auto-detected from prefix, e.g. "TODO: ...")
/note done <N>    # mark note N as done
/notes            # list active notes
/notes search X   # search notes
```

**Storage:** `<state-dir>/scribe/` — typed-year folder layout (`todos/2026/`, `handoffs/2026/`, …, each with `index.jsonl` and per-note `<seq>.md` files). Per-target state lives under `<apiary>/.repos/<name>-<id>/`, resolved automatically by the launcher. See [`PORTABILITY.md`](PORTABILITY.md) for the full state map.

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

**Artifacts** flow through per-stage directories keyed by UUID, all under the per-target state dir: `<state-dir>/runner/{intake,specs,plans,executions,hardens,reports}/`. Nothing is written into `runner/` in the checkout. The one exception is live git worktrees, which have to sit next to the repo they were cut from (`<target-repo>/.runner-worktrees/`).

**Ticket lifecycle:**
```
python -m runner.draft_ticket --title "..." --problem "..." --scope "..."   # creates backlog JSON
python -m runner.promote <slug>                                              # backlog → ready (intake)
python -m runner.run <state-dir>/runner/intake/<uuid>.json                               # runs all 6 stages
python -m runner.mark_done <slug>                                            # close a ticket hand-fixed outside the runner
```

**Cost accounting:** every stage's `<usage>` XML is piped to `budgeter/log_agent_cost.py` with the run UUID as both `session_id` and `request_id`, so `budgeter/report.py --by-request` sums the entire run as one line.

**Safety:** `NO_USAGE_STAGES` whitelists stages that legitimately make no Claude calls; any other zero-usage stage aborts the run so token caps can't be bypassed. Each handoff is gated by a validator (`validate_intake.py`, `validate_spec.py`, `validate_plan.py`).

**Overnight / detached mode:** `detached_lib.py` handles branch creation, claim-based backlog picking, hygiene prechecks, and appends to an overnight log. Cron setup is in [`runner/scheduling.md`](runner/scheduling.md). To detect and fix drift in the registered scheduled task (after repo moves, renames, or bootstrap runs), use `python -m runner.cron_health check` or `... repair --apply`; the canonical state lives in `cron_registry/<hostname>.json` (one file per machine, so a git-synced multi-machine setup does not collide).

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

## How It All Comes Together

For detailed architecture documentation, see [`docs/architecture/`](docs/architecture/):

- **[System Overview](docs/architecture/system-overview.md)** — component map, data flow, shared core, and design rationale
- **[Hook Lifecycle](docs/architecture/hook-lifecycle.md)** — PRE-to-PRE delta pattern, the Agent special case, baselines, the session-length nudge

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

Every key, its type and its shipped default is generated from the JSON files
themselves in [Config Files](docs/reference/config-files.md) — that table cannot
drift from the code; a copy here would.

---

## Reporting

```bash
python budgeter/report.py                    # grouped by session (default)
python budgeter/report.py --by-turn          # grouped by session > task
python budgeter/report.py --flat             # flat chronological list
python budgeter/report.py --all              # include zero-delta entries
python budgeter/report.py --date 2026-03-14  # single date
python budgeter/report.py --since 2026-03-01 # from date onwards
python budgeter/report.py --by-agent         # per-agent-type breakdown
python budgeter/report.py --by-request       # group by request_id (one runner run = one line)
python budgeter/report.py --weighted         # price-weight the token counts
```

---

## Testing

```bash
poetry run pytest -q                       # the whole suite (~1,700 tests)
poetry run pytest budgeter -q              # one tool
poetry run pytest --cov                    # + a coverage report (never gated)
poetry run python docs/check.py            # doc frontmatter, index, last_verified
poetry run python docs/check_cli_claims.py # cli-tools.md vs each tool's argparse
```

Every test file is a `unittest` module executed by pytest. CI runs the suite on
ubuntu/windows/macos x Python 3.11/3.12 (`.github/workflows/ci.yml`).
