---
type: architecture
title: System Overview
scope: project
description: High-level component map, data flow, and how the tools connect
framework_version: "1.0"
last_verified: 2026-08-26
---

# System Overview

## Components

```
┌─────────────────────────────────────────────────┐
│                  Claude Code                     │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ PreTool  │  │ PostTool │  │   Stop   │      │
│  │  hooks   │  │  hooks   │  │  hooks   │      │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
└───────┼──────────────┼─────────────┼─────────────┘
        │              │             │
   ┌────▼────┐    ┌────▼────┐  ┌────▼────┐
   │Budgeter │    │Budgeter │  │Budgeter │
   │  pre    │    │  post   │  │  stop   │
   │         │    │(agents) │  │(final)  │
   ├─────────┤    └─────────┘  └─────────┘
   │  Core   │
   │inject/  │
   │check    │
   └─────────┘

   ┌─────────────────────────────────────┐
   │           Slash Commands            │
   │  /startup  /note  /notes            │
   │  /budgeter  /budgeter-setup         │
   └─────────────────────────────────────┘
```

## Tool roles

| Tool | What it does | How it integrates |
|------|-------------|-------------------|
| **Budgeter** | Tracks token consumption, warns before expensive operations | Hooks (PreToolUse, PostToolUse, Stop) |
| **Scribe** | Manages cross-session notes, learnings, handoffs | Slash commands + startup skill |
| **Refiner** | Turns fuzzy ideas into structured handoff specs via adversarial questioning | Slash command (`/refine`) |
| **Harden** | Attack-defend loop that stress-tests code or plans | Slash command (`/harden`) |
| **Core** | Shared infrastructure: flags, config, session, hooks | Library imported by all tools |

## Data flow

### Per-tool-call cycle (budgeter)

```
Tool call N starts
  → PreToolUse fires
    → inject_session.py adds session context
    → check_install.py validates installation (first call only)
    → pre_tool_use.py:
        1. Reads baseline from previous call
        2. Computes delta (tokens_now - baseline) = cost of tool N-1
        3. Logs cost of N-1 to usage_log.jsonl
        4. Evaluates current assistant message for scope signals
        5. If expensive: injects warning into context
        6. Saves new baseline for next call
  → Tool N executes
  → PostToolUse fires (Agent calls only)
    → post_tool_use.py logs exact subagent token count
```

### Session lifecycle

```
Session starts
  → /startup runs (first message)
    → startup.py init: registers session identity
    → startup.py summary: loads active notes + learnings
  → ... normal work ...
  → Session ends
    → stop_session.py: logs final tool cost, cleans temp files
    → check_install_stop.py: no-op (session-scoped flags persist)
    → save_transcript.py: saves transcript for the archive
```

### Cross-session continuity

Handoffs are authored by the user at wrap-up time (`/wrapup`) and stored via scribe. The next session reads the latest handoff through `startup.py summary`, which injects it into the opening context block alongside active notes and learnings.

## Shared core

All tools import from `core/` rather than reimplementing common patterns:

- **`core/flags.py`** — feature toggles via sentinel files at `<repo>/.claude/apiary/flags/{name}-enabled`, in-process or via `python core/flags.py <toggle|enable|disable|status> <name>`
- **`core/config.py`** — JSON config loading with defaults fallback
- **`core/session.py`** — session identity (ID, role, mission) and validation
- **`core/hook_context.py`** — formatting context blocks for hook output, reading hook payloads
- **`core/hooks_lib.py`** — programmatic hook registration in `settings.json`
- **`core/utils/filelock.py`** — file locking for concurrent JSONL writes

## Design rationale

### Why hooks, not inline prompts?

Hooks run as shell commands at zero token cost. If budgeter ran as an inline prompt, every tool call would add its analysis tokens to the session — compounding cost for the entire conversation. Hooks are invisible to the token counter.

### Why scribe notes over git/comments?

Git tracks what changed, not what was deferred. Comments track local code decisions, not cross-file operational state. Scribe fills the gap: "what was I working on, what's blocked, what did I decide and why" — the things a new session needs to pick up where the last one left off.
