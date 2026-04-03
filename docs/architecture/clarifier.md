---
type: architecture
title: Clarifier v2 — Assumption Detector
scope: clarifier
description: Design rationale and data flow for the clarifier as a stateless assumption detector with caller-driven escalation
framework_version: "1.0"
last_verified: 2026-04-02
---

# Clarifier v2 — Assumption Detector

## Overview

The clarifier is a stateless agent that detects assumptions in any text an LLM is about to act on. It does not resolve assumptions — it reports them. The caller decides what to do: ask the user, refine the spec, read the codebase, or proceed anyway.

This replaces the v1 interactive clarifier, which combined detection and resolution into a multi-turn conversational agent with session logging, iteration limits, and approval flows. That complexity belonged to the caller, not the detector.

## How it works

### Interface

```
Input:
  content: string        # The spec, prompt, or plan to analyze
  context: string?       # Optional: what the content is for (helps calibrate)

Output (JSON):
  assumptions:
    - assumption: string   # What would have to be guessed
      where: string        # Which part of the input triggered it
      severity: high | medium | low
  clean: boolean           # True if assumptions array is empty
```

### Model selection

The caller chooses the model. The clarifier agent prompt is model-agnostic.

| Use case | Recommended model | Rationale |
|----------|------------------|-----------|
| Pre-execution validation | Sonnet | Needs to reason about implementation gaps |
| Quick sanity check | Haiku | Sufficient for obvious gaps, cheapest |
| High-stakes spec review | Opus | Catches subtle assumptions |

### Severity definitions

| Level | Meaning | Example |
|-------|---------|---------|
| **high** | Guessing wrong produces incorrect behavior, wrong files, or broken output | "Create the config file" — which format? where? what fields? |
| **medium** | Guessing wrong produces something that works but isn't what was intended | "Add error handling" — log and continue, or raise and abort? |
| **low** | A reasonable default exists; guessing wrong is easily corrected | "Add a docstring" — no style specified, but any style works |

### Threshold behavior

Callers should use severity to decide whether to proceed:

- **All low** — safe to auto-proceed
- **Any medium or high** — escalate to the appropriate resolver

This is a recommendation, not enforced by the clarifier. The caller owns the decision.

## What counts as an assumption

The clarifier flags points where the executor would have to **choose between alternatives without enough information to know which is correct**.

### Flag these

- Missing information needed to make a decision ("add a test" — unit test? integration test? what framework?)
- Ambiguous phrasing with two or more valid interpretations
- Implicit expectations not stated explicitly
- References to things that may not exist or may have changed
- Scope boundaries that aren't defined ("clean up the module" — which parts? how far?)

### Do not flag these

- Stylistic preferences with reasonable defaults (indentation, quote style)
- Standard conventions the executor would naturally follow
- Information that is readily available by reading a specific file (that's a lookup, not an assumption)
- Implementation details where any reasonable approach works

The distinction: an assumption is a **fork in the road where choosing wrong matters**. If all paths lead to an acceptable outcome, it's not an assumption worth flagging.

## Escalation model

The clarifier never resolves. It escalates to its caller. What happens next depends on the pipeline stage:

```
                    ┌─────────────┐
                    │  Clarifier  │
                    │  (detect)   │
                    └──────┬──────┘
                           │
                    assumptions[]
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌────────┐  ┌─────────┐  ┌─────────┐
         │  User  │  │  Opus   │  │  Agent  │
         │ (asks) │  │(refines)│  │(explores)│
         └────────┘  └─────────┘  └─────────┘
```

| Pipeline stage | Caller | Escalation target | Resolution method |
|---------------|--------|-------------------|-------------------|
| User request (pre-work) | Main agent | User | Ask directly |
| Post-refiner spec | Refiner (Opus) | Opus refiner | Refine the spec to eliminate gaps |
| Post-explorer grounding | Pipeline orchestrator | Explorer agent | Read the repo to fill in details |

The clarifier doesn't need to know who it's escalating to. It returns assumptions; the caller routes them.

## Pipeline integration

The clarifier's primary use case is the refiner pipeline:

```
User idea
  → Refine (Opus) — idea to spec
  → Explore (cheap) — ground spec in repo
  → Clarify (caller's choice) — detect remaining assumptions
      → if assumptions found:
          → Opus resolves design questions
          → Explorer resolves repo questions
          → re-run clarifier until clean
      → if clean: proceed
  → Execute (Sonnet) — implement
  → Harden (later) — stress-test
```

But the clarifier is also usable standalone — any agent can call it on any text before acting.

## Design rationale

### Why stateless?

The v1 clarifier maintained session state: logs, round counts, `.current` pointer files, UUID tracking. This existed because v1 combined detection with interactive resolution — it needed to track a multi-turn conversation.

With detection separated from resolution, there's no conversation to track. One call in, one JSON out. State management, if needed, belongs to the caller's workflow.

### Why no resolution?

Resolution requires context the clarifier doesn't have:

- **User intent** — only the user knows what they meant
- **Design rationale** — only the refiner (Opus) has the ideation context
- **Repo state** — only the explorer has read the codebase

A clarifier that tries to resolve would need all three contexts, making it expensive and complex. By only detecting, it stays cheap, fast, and composable.

### Why caller-chosen model?

Different callers have different accuracy/cost tradeoffs. A quick pre-flight check before a small edit doesn't need Opus-level analysis. A spec review for a major feature does. The caller knows which situation it's in; the clarifier doesn't.

### What this replaces

| v1 (interactive) | v2 (assumption detector) |
|-------------------|--------------------------|
| Multi-turn conversation with user | Single call, JSON response |
| Session logging (write_log.py) | No logging (caller logs if needed) |
| Iteration limits and round tracking | No iterations |
| Final approval flow | No approval (caller's responsibility) |
| Resume via agentId | No resume (stateless) |
| log_cost.py integration | Caller handles cost tracking |
| `.current` pointer file | No state files |

### What stays

- The `/clarifier` toggle and `clarifier-enabled` flag file — controls whether agents run assumption checks
- The concept of spawning a subagent for detection — keeps detection tokens out of main context
- The CLAUDE.md behavioral rules about when to invoke the clarifier — updated to match v2 interface
