---
type: standard
title: Doc Style
scope: docs
description: How to write documentation for this project — tone, format, and content rules
framework_version: "1.0"
last_verified: "2026-08-27"
---

# Doc Style

Rules for writing docs under `docs/`. See `docs/_framework.md` for the full framework definition, frontmatter schema, and templates.

## The first rule

**A doc that can drift is generated from code or tested against it; everything
else stays short.** Before writing a table of flags, hooks, config keys or
paths, check whether it is already generated — and if it is not, ask whether it
should be. A hand-maintained list of things the code also knows will be wrong
within a quarter; the repo has the receipts (review §4).

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

## Generated blocks

A table between these sentinels belongs to a generator, not to you:

```markdown
<!-- generated:start: <key> -->
| … |
<!-- generated:end: <key> -->
```

- **Never hand-edit the row set.** Change the code, then run
  `python docs/generate_cli_docs.py --write` or
  `python docs/generate_reference.py --write`. `--check` runs in
  `docs/hooks/pre-commit` and in CI.
- **Do hand-edit the prose columns.** The generators own row names and factual
  cells (a flag, a matcher, a default value, a path); the Description / Usage /
  Applies-to columns are carried over untouched, and a row with no description
  is a gap worth filling.
- **Adding a row by hand is the wrong fix** for a `--check` failure. If the
  generator wants a row you do not want, mark it:
  `<!-- cli-claims: ignore: --some-flag -->`.

What is generated today: `cli-index.md`, the CLI tables in `cli-tools.md`, the
hook registry and lifecycle events in `hooks.md`, the command list in
`slash-commands.md`, the key tables in `config-files.md`, the path table in
`file-storage.md`, and the retention table in `scribe/CLAUDE.md`.

Every ```` ```bash ```` block that invokes an apiary CLI is executed by
`docs/test_doc_examples.py` with `--help` substituted for the arguments, so an
example is a claim the suite checks. Mark a block `<!-- no-run -->` on the line
before the fence if it is illustrative rather than runnable.

## Frontmatter

Every doc must have the full frontmatter block. See `docs/_framework.md` for the schema.

Key rules:
- `description` should be specific enough to decide relevance without reading the doc
- `last_verified` is a claim: it means "I read this against the code on that date". `docs/check.py` **fails** when it is older than the file's last git commit, so a doc you edit is a doc you re-verify. Bump it only for the parts you actually checked; if you touched one section of a long doc, check the rest before stamping it
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
