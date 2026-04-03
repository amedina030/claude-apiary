# claude-apiary

A unified ecosystem of tools that extend and optimize the [Claude Code](https://claude.ai/claude-code) experience. Each tool is a specialized worker — together they form a hive.

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

### Clarifier

Automatic ambiguity detection and resolution before Claude acts on your requests.

Claude tends to make assumptions when a request is unclear — it picks one interpretation and runs with it. For consequential tasks, a wrong assumption means redoing work or losing something. Clarifier inserts a checkpoint between "you ask" and "Claude acts", specifically for requests where the assumptions matter.

**What it does:**
- Spawns when Claude detects meaningful ambiguity (judgment-based, not every request)
- Asks you targeted, numbered questions — only what is genuinely needed
- Incorporates your answers into a refined prompt, re-checks for remaining ambiguity, and repeats if needed
- Presents you the final cleaned-up prompt for explicit approval before Claude acts
- Saves a session log of every clarification round to `~/.claude/clarifier-logs/`

**When it triggers:**
- The request has multiple meaningfully different valid interpretations
- A consequential assumption about scope, target, or approach would be required
- The intended outcome isn't specific enough to verify completion

**When it stays quiet:** iterative design discussions, requests that build on prior context, straightforward tasks even if they touch multiple files.

**Toggle:**
```
/clarifier        # turn clarifier on/off
```

---

### Scribe

Structured note management for cross-session continuity.

Claude Code sessions are isolated — each one starts fresh with no memory of what happened before. Scribe bridges that gap with project-scoped notes that persist across sessions and are loaded automatically at startup.

**What it does:**
- **Notes** — typed operational state (TODOs, handoffs, blockers, decisions, wishlists, context) stored in `notes.jsonl` per project under `~/.claude/projects/<key>/`
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

**Storage:** `~/.claude/projects/<project-key>/notes.jsonl` and `learnings.jsonl` — project-scoped, git-ignored.

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
├── clarifier/                   # Ambiguity detection and resolution tool
│   ├── agents/
│   │   └── clarifier.md         # Clarifier sub-agent definition (8-step interactive flow)
│   ├── commands/
│   │   ├── clarifier.md         # /clarifier toggle command definition
│   │   └── run-clarifier-tests.md  # /run-clarifier-tests command definition
│   ├── test-suite/
│   │   ├── clarifier-test-suite.md  # 24 automated + 6 manual test cases
│   │   └── fixtures/            # Test fixture markdown files
│   ├── write_log.py             # Session log writer (init/append/complete modes)
│   ├── test_write_log.py        # Tests for write_log.py (14 cases)
│   ├── log_cost.py              # Token cost tracker (tally/finalize subcommands)
│   ├── test_log_cost.py         # Tests for log_cost.py (12 cases)
│   ├── CLAUDE.md                # Clarifier trigger rules (synced to ~/.claude/CLAUDE.md)
│   └── what-is-clarifier.md     # User-facing overview doc
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
│   ├── round_counter.py         # Round tracking for harden loops
│   └── tmp/                     # Runtime — round state files (git-ignored)
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

When the clarifier is in use, sessions that triggered it will show a token attribution breakdown:

```
Session 17c0c0df  2026-03-15 14:59:30  (1,763,590 tokens)
  [clarifier: 41,766 tokens | main: 1,721,824 tokens]
```

This is sourced from `~/.claude/clarifier-logs/cost.log`, which records session ID, token usage, and duration for every clarifier invocation.

---

## Testing

```bash
python budgeter/test_hooks.py      # unit + integration tests for budgeter
/run-clarifier-tests               # inline test runner for clarifier (24 cases)
```
