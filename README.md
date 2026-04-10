# claude-apiary

A unified ecosystem of tools that extend and optimize the [Claude Code](https://claude.ai/claude-code) experience. Each tool is a specialized worker — together they form a hive.

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

**Toggle features:**
```
/budgeter-log     # turn token logging on/off
/budgeter-warn    # turn cost estimation warnings on/off
```

---

### Scribe

Structured note management for cross-session continuity.

Claude Code sessions are isolated — each one starts fresh with no memory of what happened before. Scribe bridges that gap with project-scoped notes that persist across sessions and are loaded automatically at startup.

**What it does:**
- **Notes** — typed operational state (TODOs, handoffs, blockers, decisions, wishlists, context) stored in `notes.jsonl` inside the repo checkout at `<repo-root>/.apiary/scribe/` (under the umbrella `.apiary/` state directory, self-ignored via `.apiary/.gitignore`)
- **Learnings** — project-specific knowledge Claude discovers during task execution (workarounds, better approaches, platform quirks). Stored separately in `learnings.jsonl`, no auto-archive
- **Handoffs** — structured session summaries generated automatically on startup from the previous session's transcript, so the next session knows what happened
- **Auto-archive** — notes older than 30 days are moved to an archive file. Learnings persist indefinitely

**Commands:**
```
/note <text>      # save a note (type auto-detected from prefix, e.g. "TODO: ...")
/note done <N>    # mark note N as done
/notes            # list active notes
/notes search X   # search notes
/startup          # session init — generates handoff + loads notes and learnings
```

**Storage:** `<repo-root>/.apiary/scribe/notes.jsonl` and `learnings.jsonl` — repo-local and self-ignored via the umbrella `.apiary/.gitignore`. Gated by `APIARY_STATE_LAYOUT=repo` during the migration window (todo #268 flips the default); the legacy path `~/.claude/projects/<project-key>/` remains the fallback until then.

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

An automated attack-defend loop where an Attacker agent finds weaknesses (edge cases, vulnerabilities, design flaws) and a Defender agent fixes them. Iterates until the code or plan is hardened, producing a paper trail of what was found, fixed, and deferred.

**What it does:**
- **Attacker** reads code/plan and produces structured findings with severity ratings
- **Defender** addresses each finding: fixes, refactors, or defers with justification
- **Validators** ensure structured output — required fields, valid enums, file existence
- **IDs assigned by Python** (ATK-001, DEF-001) — deterministic, not LLM-generated
- Works on code files (with worktree isolation) or scribe plan notes

**Commands:**
```
/harden file1.py file2.py           # harden code files
/harden --plan 79                   # harden a scribe plan/spec note
/harden --focus security --deep     # focused deep analysis
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

**Overnight / detached mode:** `detached_lib.py` handles branch creation, claim-based backlog picking, hygiene prechecks, and appends to an overnight log. Cron setup is in `runner/cron_setup.md`.

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
│   │   ├── budgeter-log.md      # /budgeter-log slash command definition
│   │   ├── budgeter-warn.md     # /budgeter-warn slash command definition
│   │   └── budgeter-setup.md    # /budgeter-setup slash command definition
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
│   │   ├── attacker.md          # Attacker agent prompt template
│   │   └── defender.md          # Defender agent prompt template
│   ├── commands/
│   │   └── harden.md            # /harden slash command definition
│   ├── assign_ids.py            # Post-processor: assigns ATK-NNN / DEF-NNN IDs
│   ├── validate_findings.py     # Validates Attacker output structure
│   ├── validate_response.py     # Validates Defender output structure
│   ├── validate_and_assign.py   # Combined validate + assign-IDs step
│   ├── round_counter.py         # Round tracking for harden loops
│   └── tmp/                     # Runtime — round state files (git-ignored)
│
├── runner/                      # Autonomous 6-stage orchestrator
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
│   ├── claude_subprocess.py     # Claude CLI wrapper shared by stages
│   ├── cost_emit.py             # Emits <usage> XML from Claude envelope
│   ├── config_loader.py         # Shared config loader
│   ├── config.json              # Default orchestrator/stage config
│   ├── cron_setup.md            # Cron registration instructions
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
