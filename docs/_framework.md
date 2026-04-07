---
version: "1.0"
---

# Documentation Framework

This file defines how documentation works in the claude-apiary project. Every doc under `docs/` follows these rules. When this file changes, all existing docs must be updated to match — see [Framework Edit Workflow](#framework-edit-workflow).

## Core Principle: Errors Signal Doc Gaps

Every avoidable error is a signal that either:

1. **The docs weren't consulted.** The information existed but wasn't read before acting. This means the docs aren't loaded at the right time, or the habit of checking them isn't established.
2. **The docs don't cover this usage.** The information doesn't exist. This means the docs have a gap that should be filled.

When an error occurs that documentation could have prevented, treat it as a doc issue — not just a one-off mistake. Either update the doc to cover the missing case, or fix the loading/consultation pattern so the existing doc gets read. The goal is that the same category of error never happens twice.

## Doc Types

| Type | Purpose | Audience | Loaded when |
|------|---------|----------|-------------|
| **reference** | What exists and how to invoke it | Claude (runtime) | Startup (always in context) |
| **architecture** | Why things are structured this way | Claude + human | On demand, when touching that area |
| **standard** | How to write code and docs in this project | Claude (authoring) | When creating or modifying code/docs |
| **guide** | Step-by-step recipes for common tasks | Claude + human | On demand |

### When to use each type

- **Reference** answers "what can I call and with what arguments?" — CLI tools, hooks, slash commands, config files, storage paths. These are facts, not opinions.
- **Architecture** answers "why is it built this way?" — design rationale, data flow, component relationships. Written when the *why* isn't obvious from the code.
- **Standard** answers "how should I write this?" — code style, naming, doc format, checklists. These are opinions — the project's chosen conventions.
- **Guide** answers "how do I do X end-to-end?" — step-by-step walkthroughs for common tasks. Written when a task crosses multiple files or systems.

### When to create a new doc vs update an existing one

- If the topic fits an existing doc's scope, update it.
- If the topic is a new area (new tool, new subsystem), create a new doc.
- One doc per topic. Don't split a single topic across files. Don't combine unrelated topics.

## Frontmatter Schema

Every doc (except `_framework.md` and `_index.md`) must have this frontmatter:

```yaml
---
type: reference | architecture | standard | guide
title: Human-readable title
scope: budgeter | scribe | core | project | docs
description: One-line summary (used for relevance filtering and index)
framework_version: "1.0"
last_verified: YYYY-MM-DD
---
```

### Required fields

| Field | Purpose |
|-------|---------|
| `type` | One of: `reference`, `architecture`, `standard`, `guide` |
| `title` | Human-readable title, used in `_index.md` |
| `scope` | Which area this covers. Use `project` for cross-cutting docs |
| `description` | One-line summary. Be specific — this is used to decide relevance |
| `framework_version` | Version of `_framework.md` this doc conforms to |
| `last_verified` | Date (YYYY-MM-DD) someone confirmed the content is accurate |

## Writing Guidelines

### Tone

- Direct and specific. No fluff, no filler, no hedging.
- Lead with the useful information, not context-setting paragraphs.
- Write for someone who needs the answer now, not someone browsing.

### Format

- Use headers to create scannable structure.
- Use tables for structured data (flags, config fields, comparisons).
- Use code blocks for commands, file paths, and code snippets.
- Use bullet lists for short items. Use prose for explanations that need flow.

### What to include

- File paths (absolute from repo root, e.g. `budgeter/hooks/pre_tool_use.py`).
- Concrete examples — real commands, real output, real file content.
- Edge cases and gotchas that aren't obvious from the code.

### What to exclude

- Things derivable by reading the code (implementation details, line-by-line explanations).
- Speculation about future changes. Document what exists, not what might.
- Redundant information already in another doc — link to it instead.

## Templates

### Reference doc template

```markdown
---
type: reference
title: <Tool/System Name>
scope: <scope>
description: <one-line>
framework_version: "1.0"
last_verified: YYYY-MM-DD
---

# <Title>

<One paragraph: what this is and when you need it.>

## <Section per tool/item>

<Usage, subcommands, flags, examples.>
```

### Architecture doc template

```markdown
---
type: architecture
title: <System/Pattern Name>
scope: <scope>
description: <one-line>
framework_version: "1.0"
last_verified: YYYY-MM-DD
---

# <Title>

## Overview

<What this system does and why it exists.>

## How it works

<Data flow, component interactions, key decisions.>

## Design rationale

<Why this approach over alternatives. What constraints drove the design.>
```

### Standard doc template

```markdown
---
type: standard
title: <Convention Name>
scope: <scope>
description: <one-line>
framework_version: "1.0"
last_verified: YYYY-MM-DD
---

# <Title>

<Brief: what this standard covers and why it matters.>

## Rules

<Concrete rules. Each should be actionable and verifiable.>

## Examples

<Good vs bad examples where helpful.>
```

### Guide doc template

```markdown
---
type: guide
title: <Task Name>
scope: <scope>
description: <one-line>
framework_version: "1.0"
last_verified: YYYY-MM-DD
---

# <Title>

<When to use this guide. Prerequisites.>

## Steps

<Numbered steps. Each step: what to do, what file to touch, what to verify.>

## Checklist

<Final verification checklist.>
```

## Framework Edit Workflow

When `_framework.md` needs to change:

1. **Update content** in `_framework.md`.
2. **Bump `version`** in the frontmatter. Use semver-lite:
   - Patch (`1.0` → `1.1`): clarifications, new examples, non-breaking additions.
   - Major (`1.1` → `2.0`): new required fields, renamed types, structural changes that invalidate existing docs.
3. **Add a changelog entry** below with: version, date, what changed, which docs are affected.
4. **Run `python docs/check.py`** to identify docs with stale `framework_version`.
5. **Update affected docs** — fix content to match new rules, set `framework_version` to current.
6. **Run `check.py` again** to confirm all docs are clean.

## Changelog

| Version | Date | Change | Affected |
|---------|------|--------|----------|
| 1.0 | 2026-04-02 | Initial framework | — |
