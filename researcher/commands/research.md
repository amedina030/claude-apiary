---
name: research
description: Add, find, list, show, verify, or register tags for research findings stored per-repo under .apiary/research/
user-invocable: true
---

# /research — Research compendium

Structured per-repo store of research findings (e.g. Unreal Engine quirks, C++ gotchas, library behavior). Entries live at `.apiary/research/<topic>/<slug>.md` with YAML frontmatter (title, topic, tags, dates, sources) and a standard body (Summary, Context, Findings, Code, Caveats).

## Compendium-first rule (critical)

**Before running `WebSearch` on any topic that could plausibly be in the compendium, invoke `/research find <keywords>` first and consult the hits.** Only fall through to `WebSearch` when the compendium has no relevant entry or when the stored entry is stale (`date_last_verified` far in the past). When you finish a web investigation worth keeping, capture it with `/research add`.

Skip the compendium check for topics obviously unrelated to engineering knowledge (e.g. looking up a person, a news event, a specific URL the user provided).

## Subcommands

All subcommands dispatch to `researcher/cli.py`. Session-id is not required — state is per-repo, not per-session.

### `/research add <topic> "<title>" [--tags t1,t2,...]`
Create a new entry. Topic is a free-form category (kebab-cased automatically). Title becomes the slug. Tags must already be registered via `register-tag` — unknown tags are rejected.

```bash
python ~/.claude/apiary_launch.py researcher/cli.py add unreal "Replication basics" --tags multiplayer,networking
```

The entry is scaffolded from the template with empty sections. Fill in Summary / Context / Findings by editing the file at the path printed to stdout.

### `/research find <query>`
Ranked search across all entries. Matches title (×3), tags (×2), content (×1), returns up to 10 hits with path, title, tags, and summary preview.

```bash
python ~/.claude/apiary_launch.py researcher/cli.py find replication
```

Exit code is always 0, even on zero hits.

### `/research list [--topic X] [--tag Y]`
Show all entries grouped by topic, optionally filtered.

```bash
python ~/.claude/apiary_launch.py researcher/cli.py list --topic unreal
```

### `/research show <topic> <slug>`
Print the full entry file to stdout.

```bash
python ~/.claude/apiary_launch.py researcher/cli.py show unreal replication-basics
```

### `/research verify <topic> <slug>`
Bump `date_last_verified` to today — use after re-reading an entry and confirming it still reflects current behavior.

```bash
python ~/.claude/apiary_launch.py researcher/cli.py verify unreal replication-basics
```

### `/research register-tag <tag>`
Append a tag to `.apiary/research/tags.yaml` (the controlled vocabulary).

```bash
python ~/.claude/apiary_launch.py researcher/cli.py register-tag multiplayer
```

## Exit codes

- `0` — success (also returned by `find` with zero hits).
- `2` — validation error (unknown tag, duplicate slug, entry not found, tag already registered).
- `3` — config error (invalid YAML in `tags.yaml` or entry frontmatter).

## Typical flow

1. `/research find <keywords>` — check the compendium.
2. If no hit, investigate (WebSearch, docs, experiment).
3. `/research register-tag <tag>` for any new tag you plan to use.
4. `/research add <topic> "<title>" --tags ...` — scaffold the entry.
5. Edit the file at the printed path to fill in the findings.
