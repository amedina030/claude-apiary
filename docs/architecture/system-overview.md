---
type: architecture
title: System Overview
scope: project
description: What the toolkit is made of, how a session flows through it, and what everything shares
framework_version: "1.0"
last_verified: "2026-08-27"
---

# System Overview

## Components

One dispatcher per Claude Code event, one launcher per repo, eleven tools
behind them.

```
┌──────────────────────── Claude Code ────────────────────────┐
│  PreToolUse   PostToolUse   Stop   UserPromptSubmit         │
└───────┬────────────┬─────────┬────────────┬─────────────────┘
        │            │         │            │      one settings.json entry
        └────────────┴────┬────┴────────────┘      per event
                          │
        <repo>/.claude/apiary/launch.py            resolves main-apiary,
                          │                        exports the state dir,
                 core/hooks/dispatch.py            runs the target in-process
                          │
   ┌──────────────────────┼──────────────────────┐
   │  every hook for that event, in registry      │  → one merged
   │  order, in ONE process (docs/reference/      │    additionalContext
   │  hooks.md has the table)                     │    block
   └──────────────────────────────────────────────┘

   Slash commands, copied into <repo>/.claude/commands/ at install:
   /apiary-context /note /notes /review-learnings /wrapup /refine
   /harden /review /research /compass-sync /runner-prep /incubator
   /budgeter /budgeter-setup
```

## Tool roles

| Tool | What it does | How it integrates |
|------|-------------|-------------------|
| **core** | Install, registry, drift, the hook dispatcher, session identity, shared utilities | Library everything imports; `apiary` console script |
| **scribe** | Cross-session notes, learnings, handoffs, memory | `/note`, `/notes`, `/review-learnings`; injected at session start |
| **budgeter** | Logs what every monitored tool call cost, nudges on session length | PreToolUse / PostToolUse / Stop hooks |
| **compass** | Personality observations → a synthesized profile, plus the A/B that measures whether it helps | `/compass-sync`, `/wrapup` capture, startup injection |
| **researcher** | Durable research findings under `<state-dir>/research/` | `/research` |
| **captures** | Screenshots and images with sidecar metadata | `captures/cli.py` |
| **refiner** | Turns a fuzzy idea into a structured handoff spec by adversarial questioning | `/refine` (prompt only — no Python) |
| **harden** | Attack-defend loop over code or a plan; the control flow is Python, the skill spawns agents | `/harden` + `harden/orchestrate.py` |
| **runner** | Six-stage autonomous pipeline: intake → spec → plan → execute → harden → approval | `python -m runner.run`, cron/detached mode |
| **incubator** | Spawns a new side-project repo already wired to the toolkit | `/incubator` |
| **gui** | PyWebView desktop shell around Claude Code sessions (optional `gui` poetry group) | Standalone app |
| **docs** | The documentation framework: conformance checks, generators, the standards reminder hook | `docs/check.py`, `docs/generate_*.py`, a PreToolUse hook |
| **scripts** | Install/update entry points, the secret scanner, preflight, duplicate report | Shell + CLI, and two git hooks |
| **migrations** | Versioned upgrade scripts chained by `apiary update` | `core/update.py` |

## Data flow

### Per-tool-call cycle

```
Tool call N starts
  → PreToolUse: launch.py → dispatch.py pre  (ONE process)
      drift_check          registry catch-up if the repo moved (once/session)
      inject_session       session context (first call only)
      learnings_inject     top-3 relevant learnings (Edit|Write|Bash, flag-gated)
      research_reminder    capture nudge (WebSearch|WebFetch|Agent|Task)
      pre_push_*           doc-drift and secret gates, on `git push` only
      budgeter_pre         logs the cost of call N-1, saves baseline N
      remind_standards     standards pointer (Write|Edit, once per category)
    → the merged context block goes back to Claude Code
  → Tool N executes
  → PostToolUse: dispatch.py post
      context_rule_error_reminder   on a failed Bash call
      budgeter_post                 exact subagent cost (Agent calls)
```

Budgeter's PRE-to-PRE delta — why the cost of call N-1 is logged at call N —
is in [Hook Lifecycle](hook-lifecycle.md). The registry, matchers, ordering and
failure log are in [Hooks](../reference/hooks.md).

### Session lifecycle

```
First user message
  → UserPromptSubmit: dispatch.py prompt → startup_prompt_hook
      identity, notes summary, learnings index, the CLI index,
      the apiary toolkit rules, the compass profile
  → ... normal work ...
End of EVERY assistant turn (not session end — Stop fires per response)
  → dispatch.py stop
      budgeter_stop     logs the final call's cost, deletes the baseline
      save_transcript   records the session in <state-dir>/sessions/
```

There is no `/startup` command; startup is the hook above. Handoffs are written
by `/wrapup`, not generated automatically — the next session reads the newest
one through `core/startup.py summary`.

### Cross-session continuity

Handoffs are authored at wrap-up time and stored by scribe. `startup.py summary`
injects the latest one for the session's `(role, mission)` alongside active
notes and the learnings index. Everything is per-target: two repos never see
each other's notes.

## Shared core

Every tool imports from `core/` rather than reimplementing:

- **`core/utils/state.py`** — the state resolver. `<main-apiary>/.repos/<name>-<uid>/`, the registry, the per-repo pin files
- **`core/flags.py`** — feature toggles as sentinel files at `<repo>/.claude/apiary/flags/<name>-enabled`
- **`core/session.py`** — session identity (id, role, mission) and the once-per-session flag files
- **`core/hook_context.py`** — `HookResult`, context blocks, the standalone shim
- **`core/frontmatter.py`** — the one frontmatter dialect, for notes and docs alike
- **`core/utils/{atomic,filelock,jsonio,jsonc,gitutil,timeutil,project}.py`** — atomic writes, locking, tolerant JSON, git helpers

## Design rationale

### Why hooks, not inline prompts?

Hooks run as shell commands at zero token cost. If budgeter ran as an inline
prompt, every tool call would add its analysis tokens to the session — and
carry them for the rest of the conversation. Hooks are invisible to the token
counter.

### Why one dispatcher instead of one entry per hook?

Each settings.json entry cost a launcher process *plus* a script process. A
`Bash` call fired ~18 interpreter starts, about 1.7 s, roughly half of them
no-ops that read the payload, saw a tool name they did not care about and
printed `{}`. One entry per event, hooks in-process, is two starts and ~0.3 s.

### Why scribe notes over git or comments?

Git tracks what changed, not what was deferred. Comments track local code
decisions, not cross-file operational state. Scribe fills the gap: what was I
working on, what is blocked, what did I decide and why.
