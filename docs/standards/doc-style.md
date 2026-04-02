---
type: standard
title: Doc Style
scope: docs
description: How to write documentation for this project — tone, format, and content rules
framework_version: "1.0"
last_verified: 2026-04-02
---

# Doc Style

Rules for writing docs under `docs/`. See `docs/_framework.md` for the full framework definition, frontmatter schema, and templates.

## Tone

- **Direct.** Lead with the answer. No "In this document, we will explore..."
- **Specific.** Use file paths, function names, flag names. Not "the config file" — say `budgeter/config.json`.
- **Present tense.** "The hook logs token cost" not "The hook will log token cost."
- **Active voice.** "The hook fires before each tool call" not "Each tool call is preceded by the hook."

## Structure

- **One topic per doc.** Don't combine CLI reference with architecture rationale.
- **Headers are scannable.** A reader skimming headers should understand the doc's coverage.
- **Tables for structured data.** Flags, config fields, file paths, comparisons — use tables.
- **Code blocks for commands.** Every CLI invocation should be in a fenced code block.
- **Bullet lists for short items.** Prose for explanations that need flow.

## What to include

- **File paths** — absolute from repo root: `budgeter/hooks/pre_tool_use.py`
- **Concrete examples** — real commands with real arguments, not placeholders where avoidable
- **Edge cases and gotchas** — things that aren't obvious from reading the code
- **Cross-references** — link to other docs when relevant: `[see Hook Lifecycle](../architecture/hook-lifecycle.md)`

## What to exclude

- **Implementation details** derivable from code — don't repeat the code in prose
- **Speculation** about future changes — document what exists now
- **Redundant content** — if another doc covers it, link to it
- **Verbose preambles** — no "This document describes..." paragraphs

## Frontmatter

Every doc must have the full frontmatter block. See `docs/_framework.md` for the schema.

Key rules:
- `description` should be specific enough to decide relevance without reading the doc
- `last_verified` must be updated when someone confirms the content is still accurate
- `framework_version` must match the current version in `_framework.md`

## When to create vs update

- **Update** if the topic fits an existing doc's scope
- **Create** if it's a genuinely new area
- **Never split** a single topic across multiple files
- **Never combine** unrelated topics in one file

## Naming

- Filenames: `kebab-case.md`
- Placed in the subdirectory matching the doc type: `reference/`, `architecture/`, `standards/`, `guides/`
- Add an entry to `docs/_index.md` for every new doc
