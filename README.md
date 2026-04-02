# claude-apis

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

## Repository Structure

```
claude-apis/
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
├── setup.py                     # Unified installer for all tools
├── SETUP.md                     # Setup instructions
└── .gitignore
```

---

## How It All Comes Together

### Shared core

All tools use `core/` instead of reinventing common patterns:

- **`core/flags.py`** — every feature toggle is a sentinel file at `~/.claude/{name}-enabled`. `flags.is_enabled("budgeter-log")` replaces raw `Path` checks scattered across hooks.
- **`core/config.py`** — generic JSON config loader with a defaults fallback. Tools wrap this with their own path logic.
- **`core/hooks_lib.py`** — handles reading/writing `settings.json` and building bash-compatible hook command strings. Used by `setup.py` to register hooks without duplicates.

### Hook lifecycle (budgeter)

Claude Code fires hooks at tool lifecycle events. Budgeter uses three:

```
User turn → Claude responds → [PRE hook fires before each tool call]
                           → Tool runs
                           → [POST hook fires after Agent — logs exact subagent token cost]
                           → [PRE hook fires before next tool call — logs cost of previous]
                           → ...
                           → Session ends → [Stop hook fires — logs final tool cost]
```

The PRE-to-PRE delta pattern is key: tokens can't be measured during a tool call, so each PRE hook saves a baseline and the *next* PRE computes the delta. The Stop hook captures the last call's cost.

**Agent calls are a special case.** Subagents run in a separate transcript, so their token usage is invisible to the PRE-to-PRE delta. Instead, the POST hook reads `tool_response.totalTokens` from the payload — the exact cost reported by Claude Code — and logs it directly. The following PRE hook skips logging to avoid double-counting.

### Task chaining (`[CONT]`)

When Claude asks a mid-task clarifying question and you reply, subsequent tool calls would normally be attributed to your reply turn rather than your original request — inflating the reply turn's cost and under-attributing the original. To fix this, the hook injects an instruction into Claude's context:

> When asking a mid-task clarifying question, start your response with `[CONT]` on its own line.

When the PRE hook detects `[CONT]`, it inherits `task_turn` from the prior baseline, chaining the continuation cost back to the originating request in both the report and the warning system.

### Clarifier flow

The clarifier is implemented as a Claude Code sub-agent (defined in `clarifier/agents/clarifier.md`) rather than a Python hook. When the executing agent (Claude) detects ambiguity with the clarifier ON, it:

1. Pauses — does not begin the task
2. Spawns the clarifier sub-agent with the original prompt, its interpretation, detected ambiguities, and intended plan
3. The clarifier questions the user interactively, refines the prompt, and asks for explicit approval
4. Returns the approved prompt to the executing agent
5. The executing agent logs the clarifier session cost to `~/.claude/clarifier-logs/cost.log`, including the Claude session ID for attribution
6. Proceeds using the approved prompt, not the original

The clarifier is implemented as a subagent (not an inline prompt) so that the clarification dialogue stays out of the main session context. If it ran inline, every subsequent tool call in the session would carry those extra tokens forward — compounding cost for the remainder of the session. As a subagent, only the final approved prompt is returned.

### Session startup

On every new session, `~/.claude/CLAUDE.md` instructs Claude to run `/startup` before responding. This skill:

1. Reads the previous session's transcript (`~/.claude/.last-transcript.jsonl`)
2. Generates or consolidates a handoff note summarizing what happened
3. Loads all active notes and learnings into context

This replaces the old per-tool-call `load_notes.py` hook — notes are read once at startup instead of on every tool call.

### Scribe flow

Scribe manages two types of persistent project-scoped data:

- **Notes** (`notes.jsonl`) — operational state that decays: TODOs, handoffs, blockers, decisions, wishlists, context. Auto-archived after 30 days.
- **Learnings** (`learnings.jsonl`) — project-specific knowledge Claude discovers during execution: workarounds, platform quirks, better approaches. No auto-archive.

Both are stored under `~/.claude/projects/<project-key>/` and loaded at session start via the `/startup` skill. Claude writes notes and learnings autonomously when it encounters situations that future sessions should know about (deferred work, bugs, workarounds), or when the user explicitly asks.

### Setup

`setup.py` ties everything together at install time:
- Registers all budgeter Python hooks in `settings.json` (stripping old entries to prevent duplicates)
- Copies clarifier agent/command files to `~/.claude/agents/` and `~/.claude/commands/`
- Warns if the clarifier trigger rules are missing from `~/.claude/CLAUDE.md`

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
