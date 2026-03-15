# claude-apis

A unified ecosystem of tools that extend and optimize the [Claude Code](https://claude.ai/claude-code) experience. Each tool is a specialized worker — together they form a hive.

---

## Tools

### Budgeter

Token usage monitoring and cost estimation for Claude Code sessions.

Claude Code has no built-in visibility into how many tokens a session consumes or when a task is about to get expensive. Budgeter adds that visibility by hooking into Claude Code's tool lifecycle — silently, without spending any tokens of its own.

**What it does:**
- **Logs** token consumption per tool call to a local JSONL file
- **Warns** Claude before running a response expected to be expensive, based on similarity to past responses — Claude asks you whether to proceed
- **Chains** continuation turns (mid-task clarifying questions) back to the originating request, so costs are attributed correctly by task rather than by individual tool call
- **Reports** usage history on demand via `report.py`

**How warnings work:**
When warnings are enabled and enough task history exists, the pre-hook compares the current assistant message against historical task messages using TF-IDF cosine similarity. It finds the top-N most similar past tasks, takes their median cost, and if that exceeds your configured threshold (percentile-based or hard limit), it injects a warning into Claude's context — Claude then asks you before proceeding.

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
- Intercepts non-trivial, ambiguous requests before any work begins
- Asks you targeted, numbered questions — only what is genuinely needed
- Incorporates your answers into a refined prompt, re-checks for remaining ambiguity, and repeats if needed
- Presents you the final cleaned-up prompt for explicit approval before Claude acts
- Saves a session log of every clarification round to `~/.claude/clarifier-logs/`

**What counts as ambiguous:**
- Multiple valid interpretations of the request
- Unclear scope (how much, how far, which parts)
- A consequential assumption would be required to proceed
- The intended outcome isn't specific enough to verify completion

**Trivial requests are never intercepted** — a request is trivial only if it requires zero assumptions, has a single clearly identified target, is easily undone, and affects zero or one explicitly named file.

**Toggle:**
```
/clarifier        # turn clarifier on/off
```

---

## Repository Structure

```
claude-apis/
├── core/                        # Shared infrastructure used by all tools
│   ├── flags.py                 # Flag file management (~/.claude/{name}-enabled)
│   ├── config.py                # JSON config loader with defaults fallback
│   └── hooks.py                 # Hook registration and settings.json management
│
├── budgeter/                    # Token usage monitoring tool
│   ├── hooks/
│   │   ├── pre_tool_use.py      # Main hook: logs previous tool cost, warns if expensive
│   │   ├── post_tool_use.py     # No-op (kept for install compatibility)
│   │   └── stop_session.py      # Logs final tool cost, cleans up temp files
│   ├── lib/
│   │   ├── logger.py            # All file I/O: log, baseline, snapshot, session JSONL
│   │   └── estimator.py         # TF-IDF cosine similarity + percentile threshold logic
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
- **`core/hooks.py`** — handles reading/writing `settings.json` and building bash-compatible hook command strings. Used by `setup.py` to register hooks without duplicates.

### Hook lifecycle (budgeter)

Claude Code fires hooks at tool lifecycle events. Budgeter uses three:

```
User turn → Claude responds → [PRE hook fires before each tool call]
                           → Tool runs
                           → [PRE hook fires before next tool call — logs cost of previous]
                           → ...
                           → Session ends → [Stop hook fires — logs final tool cost]
```

The PRE-to-PRE delta pattern is key: tokens can't be measured during a tool call, so each PRE hook saves a baseline and the *next* PRE computes the delta. The Stop hook captures the last call's cost.

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
