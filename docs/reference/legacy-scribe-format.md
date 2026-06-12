---
type: reference
title: Legacy Scribe Format
scope: project
description: Sanitized format reference for the source machine's per-repo .scribe/ store — used to build the read-only legacy importer (spec §7)
framework_version: "1.0"
last_verified: 2026-06-12
---

# Legacy Scribe Format

**Format facts only.** Every real identifier, person, URL, org/team name, project name, ticket
title, GID, custom-field value, source filesystem path, and note body from the source material
has been **deliberately omitted** — they were third-party/private data and are not needed to
build the importer. Placeholders (`<target>`, `<id>`, `<gid>`) stand in for anything
site-specific. This is the engineering content lifted out of the source parity spec before that
spec was destroyed; if a value isn't here, it was private and is intentionally gone.

The source scribe stored notes per-repo under `<repo>/.scribe/`. Apiary's centralized layout
(`<state-dir>/scribe/`) is structurally identical; the importer maps one onto the other.

## Storage layout

```
<root>/<plural-type>/<year>/
    index.jsonl            # active index, one JSON object per line
    next_seq               # text file: next seq to hand out
    <seq>.md               # note body files
    archive/
        index.jsonl        # archived index
        <seq>.md           # archived bodies
```

Plural folder map: `todo→todos, handoff→handoffs, decision→decisions, wishlist→wishlists,
reference→references, blocker→blockers, context→context (no s), general→general (no s),
learning→learnings`, and source-only `ticket→tickets`. `next_seq` lives only in the active dir.
Seq gaps persist (the counter only advances). The importer reads this layout **read-only** and
never writes back.

## `index.jsonl` record schema

One JSON object per line. The importer must tolerate all fields and require none beyond the core.

| Group | Fields |
|---|---|
| Always present | `display_id, type, year, seq, status, session, timestamp, summary, brief_summary, has_body` |
| Optional | `role, mission, auto_generated, tags (list), areas (list), supersedes (str)` |
| Operation-added | `status_changed_at`, `archived_at` (present iff archived) |

Reader skips blank lines and tolerates a corrupt line by skipping it — except on seq-rebuild,
which must be strict (see apiary `scribe/notes.py` `cmd_repair`). Timestamps are ISO-8601 UTC
with explicit offset.

## Types and prefixes

`todo→T, handoff→H, decision→D, wishlist→W, reference→R, blocker→B, context→C, general→G,
learning→L`, and source-only `ticket→K`. **Apiary has no `K` type** — the importer **skips
`ticket` notes entirely** (the external Asana tool recreates them later). Display id format is
`PREFIX-YEAR-SEQ`, no zero-padding.

## Body and learning frontmatter

- Non-learning bodies: the raw content, no frontmatter.
- Learning bodies: a frontmatter block then content. Emitted keys, in order, only when present:
  `tags: [a, b, c]` and `areas: [glob1, glob2]` (inline, comma-joined, unquoted), and
  `supersedes: <id>` (scalar). The parser is best-effort and never raises; a malformed block
  returns `({}, original_text)`.

## Ticket region model (Asana tool's domain — reference only)

Documented so apiary's todo-mirror API can be shown sufficient (spec §6) and so whoever builds
the external Asana tool has the format. **Apiary implements none of this.**

- A ticket body splits on the **first** `## Asana Description` (case-insensitive) into a
  **scribe-owned** region (above) and an **asana-owned** region (below).
- Scribe-owned requires `## Summary` containing `### What` + `### Why`, and `## Acceptance
  Criteria`; optional `## Steps to Reproduce` / `## Additional Context` / `## Other`.
- Asana-owned is free-form, clobbered on pull, heading dropped on parse.
- `split_body(body) -> (scribe_owned, asana_owned)`; `compose_body(scribe_owned, asana_owned)`
  re-emits the `## Asana Description` heading between them with a trailing newline.
- On the Asana task `notes` field only (never in the local `.md`), the pushed scribe-owned
  region is wrapped by fence markers `--- scribe-managed ---` … `--- end-scribe-managed ---`;
  content outside the fence is asana-owned.

## `metadata.json` sidecar (Asana tool's domain — reference only)

Per-year `tickets/<year>/metadata.json`, a top-level object keyed by display id, serialized
`json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False)` + trailing newline.
`upsert_metadata` **deep-merges one level** (top-level keys replaced; nested `custom_fields` and
`last_asana` merged one level).

Field-name union (values omitted — the source example contained third-party data):
`asana_status, assignee_gid, assignee_name, board, classified_to, custom_fields, due_on,
from_todo, from_todo_superseded_at, from_todo_superseded_by, gid, last_asana, mirror_of_canonical,
permalink_url, projects, title, promoted_from`. `last_asana` is the 3-way-merge snapshot with
keys `{completed, custom_fields, modified_at, scribe_owned_pushed, title}`.

## todo ↔ ticket linkage

- A mirror todo links to its canonical ticket via tag `ticket:K-<id>`.
- Source cascade: marking the todo `done` marks the linked ticket `done` (no reverse path).
- For a new mirror ticket lacking a paired todo, the source auto-spawns one
  (`type=todo, auto_generated=True, tags=["ticket:<id>"]`) with a short generic body.
- **Apiary mapping:** the canonical ticket is **external**, so `K-...` is unparseable locally.
  Apiary's `mark_done` cascade must **catch parse and lookup failures and skip silently** — it
  never closes a local note. Scope the cascade to `ticket:K-*` only; never cascade to a
  parseable *local* id.

## Validator (Asana tool's domain — reference only)

Validates only the scribe-owned region: required `Summary` (with `What`, `Why`) and `Acceptance
Criteria`, non-empty; optional `Steps to Reproduce` / `Additional Context` / `Other`; flags
duplicate H2 headings. Gates **creation** only (duplicate headings block the push).

## Classification (Asana tool's domain — reference only)

Routing targets are discovered from headings in a classification config file (path is
site-specific — omitted). `general` is the default catch-all. Classification is **sticky** (once
`classified_to` is set it is never re-evaluated). Routing runs via a `claude -p` subprocess.

## `ticket-defaults.json` (Asana tool's domain — reference only)

A per-mirror `<mirror>/.scribe/ticket-defaults.json` supplies default `board` and `custom_fields`
when promoting a mirror-created ticket. Field names only; values site-specific. It is the one
file the source Asana tool reads directly rather than via the package API.

## Six-stage sync pipeline (Asana tool's domain — reference only)

Engineering outline, no data. Per scribe touch the source Asana tool runs stages 0–5b: purge
dropped → mirror→canonical body lift + close-contagion → Asana→canonical rebuild (3-way merge) →
writeback queue (body / custom-field / close diffs vs `last_asana`) → canonical→mirror propagate,
auto-spawn paired todo, promote user-created mirrors → create new Asana tasks + stamp `gid`.
Gates: validator duplicate-headings, assignee match, a recency window on closes.

## Importer mapping (what apiary actually builds — spec §7)

- Read the legacy `.scribe/` **read-only**; never modify or delete it.
- Import types `learning, handoff, todo, reference`; **skip `ticket`**.
- Create each via apiary's public API so apiary allocates fresh seq/year/timestamp.
- **Back-stamp provenance:** record each note's original `display_id` + original `timestamp` (in
  frontmatter/body and/or metadata) so ordering is recoverable and the migration is auditable.
  Carry over `tags, areas, status, session`, and (learnings) `supersedes`.
- Build and persist an **old→new id map** (reuse apiary's `migration_id_map.json` concept);
  rewrite `supersedes:` references to new ids where possible, else leave the old id visible.
- `--dry-run` ingests into a scratch state-dir and emits an equivalence report: per-type counts
  in (legacy) vs out (apiary), plus a content-diff sampling modulo the provenance stamp.
- Per the cross-machine agreement: apiary ships the importer **code** plus an optional
  **synthetic** fixture only. The real migration against real legacy data runs on the source
  machine after it pulls the prepared apiary; real data never crosses machines.
