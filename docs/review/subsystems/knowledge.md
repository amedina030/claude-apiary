---
type: architecture
title: "Knowledge tools review"
scope: project
description: Deep review of scribe, compass, researcher, captures, refiner (2026-08-26 deep review, subsystem appendix)
framework_version: "1.0"
last_verified: 2026-08-26
---

# Review: knowledge and memory cluster (scribe, compass, researcher, captures, refiner)

Read-only review of `D:\Professional\claude-apiary` at `1bee5e5` (master). Every non-test `.py` in the cluster was read in full; every `commands/*.md` in the cluster plus `core/commands/wrapup.md` and `core/commands/apiary-context.md` (the skills that actually drive these tools) was read; tests were skimmed and run. Claims below were verified by reading the code or by running throwaway probes against `tempfile` state — never against the user's real `.repos/` state (only `ls`/`grep` was used there).

---

## 1. What it is

### scribe (`scribe/`, 5.7k lines)
- **Storage**: `<state-dir>/scribe/<type-folder>/<year>/{index.jsonl,next_seq,<seq>.md,archive/{index.jsonl,<seq>.md}}` for 8 note types + `learnings/` (`scribe/store.py:22-52`). Learning `.md` bodies carry a hand-rolled frontmatter block (`tags`, `areas`, `supersedes`; `store.py:60-81`). Memory lives beside it as `memory/MEMORY.md` + `memory/*.md` but no code in this cluster reads or writes memory — it is purely a convention in `scribe/CLAUDE.md:117-152`. Legacy int IDs are resolved via `migration_id_map.json` (`notes.py:611-666`); nothing in the repo writes that file any more (grep: only readers), though it exists in the live state dir.
- **Entry points**: `scribe/notes.py` (24 argparse subcommands, `notes.py:1423-1639`), `scribe/api.py` (frozen external API used by `gui/app.py:237` and `scribe/tools/import_legacy.py:181`), `scribe/backup_indexes.py` (index snapshot), `scribe/tools/import_legacy.py` (one-shot legacy importer).
- **Surfaced to Claude**: startup banner via `core/startup.py:139-239` (active todo/wishlist/blocker/context items + latest handoff + learning count + review-staleness nudge), the learnings index via `notes.py learnings --index` injected by `core/hooks/startup_prompt_hook.py:~200-207`, per-tool-call learning injection via `core/hooks/learnings_inject_hook.py` (uses `areas` globs), and skills `/note`, `/notes`, `/review-learnings`, `/wrapup`.

### compass (`compass/`, 1.3k lines)
- **Storage**: `<state-dir>/compass/observations/<sid8>.json` (per-session JSON, validated against `compass/dimensions.json`), `observations/archive/<iso-year>-<iso-week>/`, `personality.md`, `corrections.md` (`compass/store.py:11-17`).
- **Entry points**: `observations.py` (count/list/validate/archive), `synthesize.py` (headless `claude -p`, model default `opus`, `synthesize.py:39`), `backfill.py` (headless extraction from `~/.claude/projects/<key>/*.jsonl` transcripts).
- **Surfaced**: `/wrapup` Step 4 writes an observation file by hand (`core/commands/wrapup.md:55-113`); `personality.md` is injected at every session start by `startup_prompt_hook.py:~243-262` and also by the `/apiary-context` skill (`apiary-context.md:48-57`). Cron entry `compass-weekly-synthesis` in `cron_registry/DESKTOP-JO20U69.json` runs `python -m compass.synthesize --cron` daily at 03:00.

### researcher (`researcher/`, 1k lines)
- **Storage**: `<state-dir>/research/tags.yaml` + `<topic>/<slug>.md` with a YAML-subset frontmatter parsed by `researcher/_yaml_mini.py` (`researcher/store.py:6-12`).
- **Entry points**: `researcher/cli.py` (add/find/list/show/verify/register-tag; exit codes 0/2/3 at `cli.py:34-36`).
- **Surfaced**: `/research` skill; `apiary-context.md:79-86` tells Claude to look here before `WebSearch`. Not in the startup banner.

### captures (`captures/`, 0.9k lines)
- **Storage**: `<state-dir>/captures/tags.yaml` + `<topic>/<slug>.<ext>` image + `<topic>/<slug>.md` sidecar (`captures/store.py:6-13`). Reuses `researcher._yaml_mini` (`captures/store.py:23`).
- **Entry points**: `captures/cli.py` (add/find/list/show/path/register-tag). There is **no `/captures` skill** and no `commands/` dir; it is mentioned only in `apiary-context.md:84`.
- **Surfaced**: on-demand only.

### refiner (`refiner/`, 0.4k lines)
- **Storage**: `refiner/tmp/round_<session>.json` inside the apiary checkout (not per-target state; `refiner/round_counter.py:18`). 26 stale round files currently sit there (gitignored, never cleaned).
- **Entry points**: `refiner/round_counter.py` (start/tick/reset/status). All the real logic is the 249-line `/refine` prompt.
- **`harden/round_counter.py`**: same lineage (`git log --follow`: both originate in `bb0efcd`, harden's copied in `03c224a`). harden's is a strict superset (adds `defender --set/--get`, stores a dict instead of an int). Diff is env-var name (`REFINER_TMP_DIR` vs `HARDEN_TMP_DIR`), `TMP_DIR`, and the `defender` verb — everything else is copy-paste. **Yes, duplicates.**

---

## 2. Architecture assessment

### Storage design: sound core, accreted edges
The `index.jsonl` + `<seq>.md` + typed-year folder shape is reasonable for a stdlib-only tool: listing is one small file per (type, year), bodies are git-diffable, and per-(type,year) `next_seq` avoids a global counter. `_atomic_write` (`store.py:266-282`) and `FileLock` on `_append_index`/`_write_index` (`store.py:285-307`) show the right instincts. The problems are around it:

- **Two sources of truth with no reconciliation contract.** The index carries `summary`, `brief_summary`, `has_body`, `tags`, `areas`; the `.md` carries the body and (for learnings) a second copy of `tags/areas/supersedes` in frontmatter. `get_learning` says the `.md` frontmatter "is the source of truth … the index is a mirror" (`store.py:801-804`), but `list_learnings --tag/--area` filters on the index only (`store.py:787-792`) and `learnings_inject_hook.py:75-80` matches `areas` from the index. A hand edit to a learning's frontmatter changes what `get` shows but not what is matched or listed. `repair` (`notes.py:1265-1382`) rebuilds missing index rows but never re-syncs frontmatter into existing rows.
- **The archive is a second index per year rather than a status.** `archive_note` moves a row and a file between two indexes (`store.py:621-659`), which is why every mutation path has to search two places (`get_note` `store.py:479-499`, `list_notes` `store.py:525-534`, `_read_body_any` `store.py:400-414`) and why the mutation commands silently miss archived notes (§3, bug 1). Status is *also* preserved inside the archived row, so an archived note is `{status: active, archived_at: ...}` — two orthogonal axes encoded as one folder move plus one field.
- **`migration_id_map.json`** is a third resolution path bolted onto `_parse_id_arg` (`notes.py:640-666`), with a shared key namespace for bare ints and `L<n>` (both look up `str(int)`), kept alive only because the live state still has the file.
- **Auto-archive runs as a side effect of `add`, unfiltered `list`, and startup** (`notes.py:516`, `:531-534`, `core/startup.py:153`). A read command (`list`) mutates state. This is surprising and is what makes the "done notes vanish" behaviour (§3 bug 3) feel random.

### How many frontmatter/YAML parsers?
Three distinct parsers plus one stripper, and two of them emit formats the other cannot read:
1. `scribe/store.py:_parse_learning_content` (`store.py:84-131`) — tolerant, inline `[a, b]` lists, never raises. Also reused for template `required:` (`notes.py:211-222`).
2. `researcher/_yaml_mini.py:loads/dumps` (`_yaml_mini.py:32-137`) — strict, block-style lists only, raises `YamlParseError`. Used by researcher and captures.
3. `core/hooks/startup_prompt_hook.py:_strip_frontmatter` (`~:36-48`) — skips a block, parses nothing.
4. Compass uses JSON, not YAML — the one store that got this right.

Verified cross-incompatibility (probe): `_yaml_mini.loads('tags: [a, b]')` → `{'tags': '[a, b]'}` (a string); `_parse_learning_content('---\ntags:\n  - a\n  - b\n---\nbody')` → `({'tags': ''}, 'body')` (list dropped). So a learning and a research entry are both "markdown with YAML frontmatter" that cannot be read by each other's tool. Memory files (`scribe/CLAUDE.md:130-138`) prescribe a third dialect (`name/description/type` scalars) that nothing in the repo parses at all.

### How many "resolve the state dir" helpers?
Five copies of `_git_repo_root` (`scribe/notes.py:119-143`, `compass/store.py:51-68`, `researcher/store.py:43-60`, `captures/store.py:51-68`, `core/utils/state.py:69`) — the last four are byte-for-byte identical. Six state-dir resolvers with subtly different fallbacks: `scribe_state_dir` returns `None` outside git (`notes.py:158-176`) and callers then fall back to `~/.claude/projects/<key>` (`notes.py:1618`, `backup_indexes.py:19-23`, `core/startup.py:148-150`); `compass_dir`/`research_dir`/`captures_dir` fall back to `<cwd>/.apiary/<tool>` (`compass/store.py:83`, `researcher/store.py:76`, `captures/store.py:84`); `core/utils/state.py` has the real registry resolver (`resolve_target_state_dir :253`, `find_state_dir :338`, `state_dir_from_env :327`); `startup_prompt_hook._hook_state_dir` is a seventh. Only `core/utils/state.py` should exist.

### Shared store abstraction?
No. `researcher/store.py` and `captures/store.py` differ by 103 diff lines out of 383 (`normalize_topic`, `slugify`, `read_tags`, `write_tags`, `ensure_layout`, `parse_*`, `write_*`, `_git_repo_root`, `*_dir` are identical modulo names). `researcher/cli.py` and `captures/cli.py` share `_safe_read_tags`, `_rank_hits`, `cmd_list`, `cmd_register_tag`, `_parse_*_arg`, `_today_iso`, `_build_parser` skeleton verbatim. Compass has its own third mini-store. The scribe `ScribeStore` is the only thing resembling an abstraction and it is not reused by the other two.

### Three copies of "unwrap a `claude -p` JSON envelope and strip code fences"
`notes.py:_parse_inference_response` (`:1004-1024`), `synthesize.py:_extract_markdown` (`:183-200`), `backfill.py:_extract_json` (`:180-203`). Each has slightly different fence handling.

### Three copies of the prefix table
`store.TYPE_PREFIXES` (`store.py:33-43`), `notes._PREFIX_TO_TYPE` (`notes.py:65-68`), `api.TYPE_TO_PREFIX/PREFIX_TO_TYPE` (`api.py:47-52`), plus `notes.VALID_TYPES` (`:53`) which is `TYPE_FOLDERS.keys()` minus learning, and `import_legacy.LEGACY_TYPE_FOLDERS` (`:31-35`) which is `TYPE_FOLDERS` inverted plus learnings.

### Is compass sound?
Mechanically it is the cleanest code in the cluster (JSON, a validator, a schema, exit codes, dry-run). As a *product* it is speculative: there is no measurement of whether `personality.md` changes behaviour, the only feedback channel is a free-text `corrections.md` that no tool creates or shows, and the "rolling window" never rolls because the archive sweep is manual-only (`observations.py:94-124`; no cron entry) while `synthesize` loads every active file with no cap (`synthesize.py:140-145`). The live state already has 71 observation files. The recency rule it depends on is broken by backfill (§3 bug 5). It costs one Opus call per week plus a chunk of every session's startup context. Cheap enough to keep as an experiment, but it should be labelled as one and given a kill-switch.

### Is `scribe/notes.py` a god-module?
Yes. 1643 lines, 52 commits of churn (vs 20 for `store.py`), and it contains: state-dir resolution (`:119-176`), a template-gate subsystem (`:179-255`), ANSI colour helpers (`:298-323`), auto-archive policy (`:326-381`), 24 command handlers, an LLM inference client (`:949-1024`), a repair tool (`:1265-1382`), a one-shot migration (`:1198-1262`), and a 217-line `main()`. Policy (what auto-archives when) lives in the CLI module rather than the store, so `core/startup.py` has to import `run_auto_archive` from the CLI (`startup.py:22-25`).

### What should be consolidated
1. One `core/utils/state.py` resolver; delete the four `_git_repo_root` and the per-tool `*_dir` fallbacks.
2. One frontmatter module (`core/frontmatter.py`) with one dialect used by scribe learnings, researcher, captures, memory, and templates.
3. One `SidecarStore` (topic/slug/tags.yaml/frontmatter) that researcher and captures both instantiate.
4. One `round_counter.py` in `core/` (or refiner imports harden's).
5. One `claude_envelope.py` helper for the three envelope parsers.
6. Move auto-archive policy, repair, and backfill into `scribe/store.py` (or a `scribe/maintenance.py`); leave `notes.py` as argparse + printing.

---

## 3. Bugs and correctness risks (ordered by severity)

**1. `done`/`drop`/`defer`/`resume`/`update` on an archived note report success and change nothing.** `cmd_done` fetches via `get_note` (which searches the archive, `store.py:488-498`), then calls `store.update_note` which scans only the *active* index (`store.py:590-619`) and returns `None`; the return value is ignored and "Marked X as done." is printed (`notes.py:741-749`). Same pattern in `cmd_drop :768`, `cmd_defer :791`, `cmd_resume :808`, `cmd_update :869`. Verified in a temp store: `cmd_done` printed `Marked T-2026-1 as done.` and the note stayed `status=active, _from_archive=True`; `cmd_update --content NEW` printed `Updated T-2026-1.` and the body was unchanged. Because handoffs are auto-archived aggressively (all but the latest, `notes.py:361-364`) and done notes archive one day after *creation* (bug 3), this is hit in normal use, not just edge cases.

**2. `researcher/_yaml_mini` corrupts values containing `:` or `#`, compounding on every `verify`.** `dumps` quotes such values (`_yaml_mini.py:96-117`) but `loads` never unquotes (`:72-92`) and treats any `#` as a comment start (`:47-49`). Verified: `'Foo: bar'` round-trips as `'"Foo: bar"'`; `'C# generics'` → `'"C'`; `'https://example.com/a#frag'` → `'"https://example.com/a'`. Running `loads(dumps(...))` three times on `'C# generics'` yields `'"C'` → `'"\\"C"'` → `'"\\"\\\\\\"C\\""'`. `cmd_verify` (`researcher/cli.py:256-273`) is exactly `loads → mutate → dumps`, so every `/research verify` on an entry whose title has a colon, or whose `sources:` list holds a URL with a fragment, degrades the file. Live exposure: 2 of the entries under `.repos/claude-apiary-1/research/` already have quoted titles (e.g. `title: "Claude Code GUI: interactive-wrapper vs Agent-SDK billing"`). The skill (`research.md:28`) explicitly tells the user to hand-edit the file, and the `sources` field exists to hold URLs, so this is the expected input, not adversarial input. Captures inherits it (`captures/store.py:23`).

**3. "Done" notes auto-archive on the next command if the note is older than one day — measured from creation, not completion.** `_run_auto_archive_store` compares `timestamp` (creation) to `done_cutoff` (`notes.py:355-360`), not `status_changed_at`. Verified: a note created 5 days ago and marked done just now was archived by the very next auto-archive pass. Docstring says "done - archive after 1 day" (`:333`). Combined with bug 1 the sequence `done T-1` → `list` (auto-archive) → `update T-1 --add-tag x` silently does nothing.

**4. Lost-update race between any two scribe processes on the same `(type, year)`.** `_append_index` is a locked read-modify-write (`store.py:296-307`), but `update_note`, `archive_note`, `unarchive_note`, `archive_learning`, `remove_learning` all read the index *outside* the lock and then `_write_index` the whole list under the lock (`store.py:590/617`, `:636/646`, `:674/686`, `:845/855`, `:873/882`). Interleaving: A reads `[T1,T2]`; B `add` appends `T3` (locked); A writes `[T1',T2]` (locked) → `T3`'s index row is gone while `3.md` remains. `repair` would later resurrect it with `session=''` and an mtime timestamp (`notes.py:1320-1331`). Two sessions running `/wrapup` concurrently (each does `add`, and each `add` triggers auto-archive of the *other's* handoff via `archive_note`) is a realistic trigger. `TestConcurrency` (`test_store.py:205-231`) only exercises concurrent `add`.

**5. Compass recency weighting is inverted by backfill.** `backfill.py` stamps `captured_at = now` (`:224`, `:248`) — the time of backfill, not the session. `synthesize.py` sorts observations by `captured_at` (`:50`) and tells the model "prefer the more recent one (compare `captured_at`)" (`:78`) and to use only the last 5 sessions for volatile dimensions (`:74`). So backfilling a six-month-old transcript today makes it the most authoritative evidence, and five backfilled old sessions displace every real recent `mood_tone` signal. The transcript mtime is available (`_select_transcripts` uses it, `:110`) and should be used instead.

**6. `add_note` writes the index row before the body; a crash between them turns into data loss on `repair`.** `store.py:466-467` (`_append_index` then `_write_note_file`). After a crash the row has `has_body: true` and no `.md`; `get_note` flags `_warning: body_file_missing` (`:484-485`) but `cmd_repair` treats an index row without an `.md` as an orphan and deletes it (`notes.py:1336-1347`). Writing the body first (atomically) would make repair's rebuild path recover instead.

**7. `archive_note` is a three-step non-transactional move.** Order: rewrite active index without the row (`store.py:646`) → append to archive index (`:651`) → copy-then-unlink the `.md` (`:653-657`). A crash after step 1 loses the row from both indexes; after step 3's `write_text` but before `unlink`, the body exists in both dirs and `repair` rebuilds a phantom *active* duplicate (`notes.py:1312-1333`). `os.replace` for the file move and archive-append-before-active-remove (so the worst case is a duplicate row, which is detectable) would fix it.

**8. `/notes learning` is a guaranteed argparse error.** `notes.md:21` maps it to `notes.py list --type learning`, but `--type` choices are `VALID_TYPES` (`notes.py:1476`, `:53`) which excludes `learning`. Verified: `error: argument --type: invalid choice: 'learning'`. `docs/reference/cli-tools.md:50` documents `learning` as a valid `--type` too.

**9. `/review-learnings` stamps `last_review` in the wrong directory, so the startup nudge never clears.** Step 5 (`review-learnings.md:63`) runs `python -c "from scribe.notes import scribe_state_dir; ..."` *without* the launcher: (a) `scribe` is not importable unless cwd is the apiary root; (b) even then `APIARY_TARGET_STATE_DIR` is unset, so `scribe_state_dir()` falls to `<git-root>/.apiary/scribe/` (`notes.py:173-176`) — the pre-migration in-repo path — while `core/startup.py:126` checks `<registry-state-dir>/learnings/last_review`. Line 66's claim that "it resolves the per-target state dir via the registry" is false. Step 4 (`:57`) runs `python scripts/retrotag_learnings.py` the same way.

**10. Every `notes.py learn` without `--tags` spawns a `claude -p` subprocess.** `cmd_learn :932-935` → `_infer_learning_tags_areas :949-986` → `run_claude(timeout=10)`. `/wrapup` (`wrapup.md:20`) and `scribe/CLAUDE.md:108` invoke `learn` without `--tags`, so each captured learning is an LLM call with a 10 s budget, on the critical path of wrapup, with a stderr warning on failure that the model will usually ignore. No test covers this path (grep: no `cmd_learn(` in tests). `--tags`-less `supersede` does the same (`:1161-1164`).

**11. `ScribeStore.__init__` eagerly creates the whole layout, and it is constructed on the PreToolUse hot path.** `ensure_layout` (`store.py:210-230`) does ~45 `mkdir/exists/write` calls per construction; `core/hooks/learnings_inject_hook.py:221` constructs a store on every Edit/Write/Bash. It also means any code path that resolves the wrong dir (bug 9, or `notes.py:1618`'s `~/.claude/projects/<key>` fallback) silently materialises a full scribe tree there.

**12. `_parse_learning_content` inline-list parsing splits on commas inside quotes** (`store.py:124`; verified `[a, "b, c"]` → `['a','b','c']`), and a *body* that starts with a markdown horizontal rule `---` and contains a later `---` line will have everything between swallowed as frontmatter (`:95-106`). Only learnings and templates go through it, so low blast radius.

**13. Legacy importer never persists the id map.** `import_into` returns `id_map` (`import_legacy.py:139`) and `main` only prints its length (`:176`, `:183`); `docs/reference/legacy-scribe-format.md:118` requires "Build and persist an old→new id map". Cross-references in note bodies (`T-2026-5` mentioned in a handoff) are not rewritten and there is no artifact to rewrite them later.

**14. Compass `archive` uses file mtime, not `captured_at`** (`observations.py:84-91`), so a `backfill --force` or a file copy resets the clock; `synthesize --cron` also throttles on `personality.md` mtime (`synthesize.py:131-138`).

**15. Minor / Windows-specific**
- `FileLock` leaves `index.jsonl.lock` and `next_seq.lock` litter in every year dir forever (`core/utils/filelock.py:15,21`; live `todos/2026/` has 2). On Windows, `os.replace` in `_atomic_write` (`store.py:276`) fails with `PermissionError` if another process holds `index.jsonl` open — `gui/scribe_aggregator.py:135-137` iterates the file with it open, so scribe writes can fail while the GUI sidebar refreshes. Old file stays intact; the write is lost with a traceback.
- `_increment_seq` locks `next_seq`, but `_ensure_year_dir` → `_rebuild_next_seq` runs unlocked (`store.py:315-352`); two processes creating the first note of a new year can both rebuild and both get seq 1.
- `researcher/cli.py cmd_list --topic` compares the raw arg to the normalised stored topic (`:223`) — `--topic Unreal` matches nothing.
- `captures/cli.py cmd_add --move` moves the image (`:125-126`) before writing the sidecar (`:145`); a sidecar failure leaves an orphaned image and the source is gone.
- `captures/store.find_image` returns whichever extension `iterdir` yields first when two exist (`:114-120`).
- `refine.md:75-77` puts `\n` inside a bash double-quoted string; bash does not interpret it, so the decision note body contains literal `\n`.
- `backfill.py:227-231` rebuilds the full prompt per dropped message (O(n²) string building on 200 KB prompts).

---

## 4. Data safety

**Commands that destroy data with no undo:**
- `notes.py unlearn` hard-deletes the `.md` and index row (`store.py:867-887`). `scribe/CLAUDE.md:113-114` and the `notes.py` usage block still advertise it as the way to "remove a stale learning"; only `/review-learnings` steers toward `archive-learning`.
- `notes.py update --content` overwrites the body with a plain `write_text` (`store.py:387-390`) — non-atomic, no prior-version retention. `cli-tools.md:96` claims bodies are "append-only"; they are not.
- `cmd_repair` without `--dry-run` removes orphan index rows (`notes.py:1336-1347`) — correct for true orphans, destructive after bug 6.
- `backup_indexes.create_backup` `rmtree`s the same-day snapshot before writing (`backup_indexes.py:27-29`); running it twice in a day discards the morning copy.
- `compass/backfill.py --force` overwrites an inline `/wrapup` observation (the higher-quality one) with an LLM-extracted one (`:212-215`).
- `researcher verify` silently rewrites the whole file (`cli.py:271`) — with bug 2 that is a corruption vector, not just a rewrite.

**Backup/restore:** `scribe/backup_indexes.py` snapshots only `index.jsonl` files and `migration_id_map.json` (`:32-52`) — not `next_seq`, not `.md` bodies, not compass/research/captures. There is no restore command; restoring means hand-copying and then `repair`. It is not scheduled anywhere (`cron_registry/DESKTOP-JO20U69.json` has two entries, neither is backup); the live state has exactly one snapshot, `backups/2026-04-11`, i.e. it has run once in four months. `cli-tools.md:96` says "Intended to run on a daily cron" — aspirational.

**Crash mid-write:** index writes are atomic and locked (`store.py:266-307`) — good. Body writes are not (`:387-390`); `personality.md` is not (`synthesize.py:177`); researcher/captures `write_entry`/`write_sidecar` say "atomically" in their docstrings (`researcher/store.py:158`, `captures/store.py:187`) and are plain `write_text`. Multi-file operations (add, archive, unarchive, supersede) have no ordering that makes the crash state recoverable by `repair` (§3 bugs 6–7). `supersede` archives the old learning *before* writing the new one (`notes.py:1167-1184`); a failure in `add_learning` (e.g. the inference timeout is caught, but an `OSError` is not) leaves the old learning archived with no replacement.

**Silent invisibility:** researcher and captures `find`/`list` `continue` past any entry that fails to parse (`researcher/cli.py:152-154`, `:218-220`; `captures/cli.py:158-160`, `:230-233`). A hand-edited entry that `_yaml_mini` rejects (nested map, indented scalar, quoted string with a colon) disappears from the compendium with no warning, defeating the "check the compendium before WebSearch" rule.

---

## 5. Code quality

**Five largest functions** (AST-measured):
| Lines | Function | Location |
|---|---|---|
| 217 | `main` | `scribe/notes.py:1423-1639` — argparse for 24 subcommands + identity auto-fill + dispatch |
| 128 | `cmd_add` | `scribe/notes.py:389-516` — dedup guard, content-file, 3 length gates, template gate, unique-tag, metadata, add, auto-archive |
| 118 | `cmd_repair` | `scribe/notes.py:1265-1382` — 7 nesting levels (type → year → active/archive → entries/md → next_seq → archive index) |
| 79 | `cmd_add` | `captures/cli.py:72-150` |
| 72 | `cmd_list` / `_build_prompt` | `scribe/notes.py:519-590` / `compass/synthesize.py:54-125` |

**Dead or vestigial code (callers grepped repo-wide):**
- `cmd_migrate` — a stub that always exits 1 "planned for Phase 3" (`notes.py:898-901`), still registered (`:1528-1529`) and documented as "Run data migrations" (`cli-tools.md:35`).
- `_repo_scribe_dir` (`notes.py:146-155`) — no callers anywhere.
- `scaffold_handoff_template` (`notes.py:239-255`) — only called from `scribe/test_template_gate.py:57,85`; docstring admits "Not called automatically". The live state has no `templates/` dir, so the entire template-gate subsystem (`notes.py:55-63`, `:179-236`, `:447-477`, `:1385-1416`, `:1461-1472`, 12 tests) is dormant code.
- `handoff-sessions` subcommand (`notes.py:1086-1097`) — no callers outside archived 2026-03 handoffs.
- `api.format_display_id` — only tests call it.
- `notes.py:30` `from pathlib import Path as _PathImport` and `:37` `from pathlib import Path` — same import twice; `_PathImport` used once at `:33` and once inside a function at `:958` (a `sys.path.insert` repeated on every call).
- `notes.py:5` module docstring describes storage as `.claude/notes.jsonl` / `notes_archive.jsonl` — two layouts ago.
- `notes.py:1489`, `:1562` help strings: "Note ID (integer) or learning ID (L-prefix, e.g. L3)".
- `store.py:311-313` `_year_dir` — no callers.
- `import re` in `api.py` used; `os` imported in `store.py:8` used only for `os.fdopen/replace/unlink` — fine.

**Copy-paste:** quantified in §2 — `researcher/store.py` vs `captures/store.py` (103 diff lines of 383), `researcher/cli.py` vs `captures/cli.py` (225 diff lines of 719, mostly the add/show/path bodies; helpers verbatim), four identical `_git_repo_root`, three envelope parsers, two round counters, three prefix tables.

**Naming/consistency nits:** `store.list_notes` raises `KeyError` for a bad type (`store.py:512`, with a comment saying so) while `_type_dir` raises `ValueError` (`:379`); `_WRITE_COMMANDS = {"add","learn","update"}` (`notes.py:1605`) omits `supersede`, which accepts `--role/--mission` (`:1579-1580`) but never gets them auto-filled; `derive_summary` hard-codes 300 (`store.py:194`) while `notes.MAX_SUMMARY_LENGTH = 300` (`:72`) exists; `researcher/store.ENTRY_FIELDS` (`:31-38`) and `captures/store.ENTRY_FIELDS` (`:37-46`) are declared and never read. No `TODO/FIXME/HACK` markers in the cluster (grep clean). 30 lines >100 chars in `notes.py`.

**Good parts worth keeping as-is:** `store._atomic_write`/`_append_index` (`store.py:266-307`), strict `_read_index` for seq rebuild (`:235-263`, `:332-352`), `derive_brief_summary` (`:134-181`, well-commented heuristics with tests), `compass/store.validate_observation` (`:140-195`), `api.normalize_entry` (`api.py:174-204`), `import_legacy.read_legacy_notes` (`:55-74`).

---

## 6. Tests

`poetry run pytest scribe compass researcher captures refiner -q` → **286 passed in 17.22 s** (harden's `test_round_counter.py` was not in the requested set; it is also green when run separately, 11 tests).

**`scribe/test_notes.py` "standalone runner":** false. It is a plain `unittest.TestCase` module with an `if __name__ == '__main__': unittest.main()` guard (`test_notes.py:412-413`); pytest collects all 47 tests (`--collect-only` confirmed). Note `docs/standards/code-style.md:96` still says "Use unittest (stdlib). No pytest." while `pyproject.toml:26,43-44` declares pytest and its ini options.

**Hermeticity:** good across the board. Every scribe test uses `tempfile.TemporaryDirectory()` and constructs `ScribeStore(tmp)` directly; researcher/captures patch `store._git_repo_root` (`test_researcher.py:25-28`, `test_captures.py:39`); compass patches `APIARY_TARGET_STATE_DIR` (`test_store.py:134-163`); refiner/harden round-counter tests spawn a real `python` subprocess with `*_TMP_DIR` pointed at a temp dir. Nothing touches `~/.claude` or `.repos/`. Nothing spawns `claude`.

**Coverage gaps (by reading, not by tool):**
- **Zero tests** for `compass/synthesize.py`, `compass/backfill.py`, and the `compass/observations.py` CLI (archive sweep, `validate` filename check). `compass/test_store.py` covers only `store.py` (24 tests).
- `notes.py`: no test for `cmd_learn`/`cmd_supersede` (the inference path), `cmd_archive` (manual `--before`), `cmd_get`'s legacy-id branches (`:640-666`), `cmd_done` on an archived note (would have caught bug 1), `_run_auto_archive_store`'s handoff and context rules (`test_notes.py:102-121` covers done and decision only), `cmd_handoff_sessions` beyond a smoke test, `--unique-tag`, `--if-no-handoff-for` against archived handoffs.
- `store.py`: no test for concurrent `update`+`add` (bug 4), crash ordering (bug 6/7 — `test_atomic_index.py` covers only the index temp file), `_parse_learning_content` with quoted/comma items, learning frontmatter vs index divergence.
- `_yaml_mini`: `test_roundtrip_basic_frontmatter` (`test_researcher.py:225`) and `test_raises_on_unparseable` (`:239`) only; nothing with `:`/`#`/quotes/URLs (bug 2).
- `backup_indexes`: create/prune/main covered (6 tests); no restore because none exists.
- `import_legacy`: 9 tests with a synthetic fixture — decent; no test that `supersedes` is rewritten across the archive boundary or that the id map is persisted (it isn't).
- Test file naming: `test_cascade.py`, `test_tags.py`, `test_search_breadth.py`, `test_status_changed_at.py`, `test_year_counters.py` are feature-named, contradicting `code-style.md:97` (`test_<module>.py`). Not harmful, but the standard is not enforced.

---

## 7. Skills review

Installed copies under `.claude/commands/` are byte-identical to the source files (diff -q clean for all seven).

**`scribe/commands/note.md` (33 lines)** — Precise and short. Problems: (a) `HANDOFF` prefix → `add --type handoff` with no `--summary`, which `notes.py:423-429` rejects as a hard error; the skill never mentions `--summary`. (b) No `GENERAL:` prefix although `general` is a valid type and `scribe/CLAUDE.md:75` describes it as the default bucket; the skill defaults to `context` instead (`note.md:20`) — the two docs disagree on the default. (c) `--content "<content>"` in bash; `apiary-context.md:90-104` says to use list-form subprocess or `--content-file` for anything with backticks/apostrophes. Not a runaway risk.

**`scribe/commands/notes.md` (35 lines)** — `/notes learning` is broken (bug 8). Missing mappings for `--deferred`, `done`, `drop`, `defer`, `resume`, `unarchive`, `--role/--mission`. Otherwise matches argparse.

**`scribe/commands/review-learnings.md` (74 lines)** — Steps 1–3 match the CLI (`learnings --index`, `get`, `archive-learning`, `supersede` all exist with those flags). Step 4 and Step 5 bypass the launcher and are broken for registered targets (bug 9). Step 5 is also the one place a skill embeds Python in a `-c` one-liner instead of a CLI verb — a `notes.py mark-reviewed` command would remove the whole problem. Line 29 asks to use `AskUserQuestion`, which the user's feedback memory says not to do. Line 74's "archive is reversible only by manual file moves today" is true for learnings (no `unarchive-learning`), and the skill correctly requires per-entry approval — no data-loss instruction. No loop risk.

**`compass/commands/compass-sync.md` (48 lines)** — Accurate: flags, exit codes and the `core/utils/state.py` path-print all exist (`synthesize.py:203-213`, `state.py` `__main__`). Good length. Only nit: Step 1's "if count is 0 … stop" duplicates `synthesize.py`'s own exit-1 check.

**`researcher/commands/research.md` (79 lines)** — Matches `cli.py` verbs, flags and exit codes exactly. The "compendium-first" rule is clear. Line 28 instructs hand-editing the file, which is where bug 2 bites; the skill should either warn about `:`/`#` in frontmatter or the parser should be fixed. Missing: any `/captures` counterpart (there is no skill for captures at all).

**`refiner/commands/refine.md` (249 lines)** — Long but well-structured; the round-counter calls match `round_counter.py`. Internal contradiction: "9 validation rules" (`:202`) vs "Re-run the 8 validation rules" (`:225`). `--print-repo-path` exists (`core/launcher_template.py:88-91`). The 15-round cap is a soft prompt to the user, so the loop is bounded. Data-loss risk: Step 5 (`:236-238`) saves the full handoff through bash `--content "<full handoff text>"` — a handoff containing backticks or `$(...)` will be shell-expanded or fail; `--content-file` exists precisely for this (`notes.py:1434-1437`) and is not mentioned. Step 1 idea-kill (`:75-77`) has the literal-`\n` bug. Step 2's "zero file reads" rule and Step 2.5's Explore subagent are sensible.

**`core/commands/wrapup.md` (113 lines, drives scribe + compass)** — Step 3's handoff structure (`### What was done / Key decisions / What's pending / Where it stopped`, `:43-51`) does not match `scribe/default_templates/handoff.md`'s `required: [shipped, open, pick up next session with]`. Today that is harmless because nothing scaffolds the template; the moment someone runs `scaffold_handoff_template` every `/wrapup` handoff will be rejected by the gate (`notes.py:468-477`) and the model will reach for `--force`. Step 2 `learn` without `--tags` triggers an LLM call per learning (bug 10). Step 3 has the same bash-quoting exposure as refine. Step 4 (compass) is precise and its validate command is correct; it has an explicit "never block wrapup" rule, which is right. `compass/CLAUDE.md:112` says "Don't write observation files manually", which contradicts the fact that `/wrapup` is exactly that — the intent is "don't fabricate", and the wording should say so.

**Missing skill:** captures has none; `apiary-context.md:84` is its only surface.

---

## 8. Docs vs reality

| Doc | Claim | Reality |
|---|---|---|
| `README.md:70` | Handoffs are "generated automatically on startup from the previous session's transcript" | Nothing at startup writes a handoff; `core/startup.py` only reads. Handoffs come from `/wrapup` Step 3. |
| `README.md:71`, `scribe/CLAUDE.md:17,169` | "notes older than 30 days are moved to an archive folder" | Actual policy (`notes.py:326-372`): context 3 d, decision 30 d, done 1 d after *creation*, handoffs all-but-latest immediately, todo/wishlist/blocker never. |
| `README.md:79` | `/startup` command | No such skill exists (no `startup.md` anywhere). |
| `README.md:82`, `docs/reference/file-storage.md:67` | `APIARY_STATE_LAYOUT=legacy` escape hatch | Removed; `notes.py:47-49` says so and no code reads the variable (grep). |
| `README.md:222-343` tree | scribe = `commands/`, `notes.py`, `test_notes.py` | Omits `store.py`, `api.py`, `backup_indexes.py`, `tools/import_legacy.py`, `default_templates/`, 13 test files; omits `captures/` entirely. README has no Captures section. |
| `scribe/CLAUDE.md:19`, `file-storage.md:63` | Archive at `<type>/archive/` / `scribe/<type>/archive/<year>/` | Actual: `<type>/<year>/archive/` (`store.py:226`, `:344`). |
| `scribe/CLAUDE.md:107-114` | `python scribe/notes.py learn …` | Contradicts the launcher rule in `apiary-context.md:7-13`; these are the commands the model copies. |
| `scribe/CLAUDE.md:113` | `unlearn` = "Remove a stale learning" | Hard delete; `/review-learnings` and `archive-learning` are the intended path. |
| `compass/CLAUDE.md:59` | Cron lives in `runner/cron_registry.json` | It is `cron_registry/<hostname>.json` (`cli-index.md:45`). |
| `compass/CLAUDE.md:90-92`, `README.md:133` | "rolling 50-session window with archive" | Sweep is manual-only; nothing schedules `observations.py archive --apply`. |
| `compass/CLAUDE.md:112` | "Don't write observation files manually" | `/wrapup` Step 4 writes them manually by design. |
| `cli-tools.md:35` | `migrate` — "Run data migrations" | Stub that always errors (`notes.py:898-901`). |
| `cli-tools.md:50` | `--type` accepts `learning` | It does not (`notes.py:53`, `:1476`). |
| `cli-tools.md:81-96` | backup copies "the global `next_id` counter"; bodies are "append-only"; "intended to run on a daily cron" | Copies `migration_id_map.json` not `next_id` (`backup_indexes.py:49-52`); no `next_seq` copied; bodies are rewritten by `update` and deleted by `unlearn`; not scheduled. |
| `cli-index.md` | scribe subcommands: 9 listed | 24 exist; `researcher/cli.py` and `captures/cli.py` are absent from the index (grep: no `captures` in file). |
| `docs/reference/legacy-scribe-format.md:118` | importer must "persist an old→new id map" | Never written (`import_legacy.py:172-184`). |
| `docs/standards/code-style.md:96-97` | "No pytest"; `test_<module>.py` naming | `pyproject.toml:26,43`; feature-named test files. |
| `docs/reference/slash-commands.md:13` | commands load from `~/.claude/commands/` | This repo installs into `<repo>/.claude/commands/`. |
| `notes.py:5` docstring | `.claude/notes.jsonl` storage | Two layouts stale. |
| `researcher/store.py:158`, `captures/store.py:187` | "Write an entry file atomically" | Plain `write_text`. |
| `refiner/commands/refine.md:225` | "8 validation rules" | There are 9 (`:202-214`). |

---

## 9. Verdicts

| Component | Verdict | Reason |
|---|---|---|
| `scribe/store.py` | **improve** | Right shape; fix write ordering (§3 6–7), lock read-modify-write (§3 4), make `update_note` archive-aware or make callers check the return. |
| `scribe/notes.py` | **rewrite (split)** | God-module; move policy/repair/backfill/templates/inference out, leave argparse + printing. Delete `migrate`, `_repo_scribe_dir`, `handoff-sessions`, stale docstrings. |
| `scribe/api.py` | **keep** | Small, frozen, tested, has real consumers (GUI, importer). Fold the prefix table into one place. |
| `scribe/backup_indexes.py` | **improve** | Back up `next_seq` and bodies (or the whole `scribe/` tree — it is tiny), add `restore`, schedule it, fix its doc. |
| `scribe/tools/import_legacy.py` | **keep** | One-shot, tested; persist the id map before it is run for real. |
| Template gate (`notes.py:55-63,179-255,447-477,1385-1416`) | **delete or finish** | Dormant: nothing scaffolds a template, and `/wrapup`'s handoff shape contradicts the bundled template. Either wire it (and fix wrapup.md) or remove ~150 lines + 12 tests. |
| Legacy int-ID resolution (`notes.py:611-666`) | **delete after migration check** | Nothing writes `migration_id_map.json`; keep only if the live map is still referenced by old notes. |
| `scribe/commands/note.md`, `notes.md` | **improve** | Fix `/notes learning`, handoff `--summary`, add `general`, mention `--content-file`. |
| `scribe/commands/review-learnings.md` | **improve** | Replace Steps 4–5 with launcher-invoked CLI verbs (`notes.py mark-reviewed`). |
| `compass/store.py`, `observations.py` | **keep** | Cleanest code in the cluster. |
| `compass/synthesize.py` | **improve** | Cap observation count, atomic write, add `--max-sessions`; add tests. |
| `compass/backfill.py` | **improve** | Stamp `captured_at` from transcript mtime (§3 5); add tests. |
| Compass as a product | **keep as labelled experiment** | Cheap, but unmeasured; give it a flag and a scheduled archive sweep, or it will grow without bound. |
| `researcher/_yaml_mini.py` | **rewrite** | Round-trip asymmetry corrupts real data (§3 2). Either symmetric quote handling + no `#`-in-value comments, or switch to JSON frontmatter / a single shared frontmatter module. |
| `researcher/store.py` + `captures/store.py` | **merge into `core/sidecar_store.py`** | 73 % identical. |
| `researcher/cli.py` + `captures/cli.py` | **merge** helpers, keep two thin CLIs | Verbatim-shared `_rank_hits`, `_safe_read_tags`, `cmd_list`, `cmd_register_tag`. |
| `captures/` | **keep + add skill** | Works, tested, but invisible without a `/captures` skill. |
| `refiner/round_counter.py` | **delete → use `harden/round_counter.py`** (or move to `core/`) | Strict subset of harden's; same lineage. |
| `refiner/commands/refine.md` | **improve** | Fix 8-vs-9, `\n` literal, use `--content-file`. |
| Four `_git_repo_root` + per-tool `*_dir` resolvers | **delete → `core/utils/state.py`** | One resolver already exists. |
| `scribe/CLAUDE.md`, `compass/CLAUDE.md`, README scribe/compass sections, `cli-tools.md` scribe/backup sections | **improve** | Table in §8. |

---

## 10. Top 10 recommended changes (value ÷ effort)

1. **Make mutations archive-aware or fail loudly** — `update_note` should search the archive dir too (or return `None` and every `cmd_*` must check it and `sys.exit(1)`). Fixes §3 bug 1. **S**
2. **Fix `_yaml_mini` round-trip** — unquote on load, only treat `#` as a comment when preceded by whitespace/at line start, and add round-trip tests for `:`, `#`, URLs, quotes. Fixes live corruption on `verify`. **S**
3. **Archive "done" by `status_changed_at`, not `timestamp`**, and stop auto-archiving inside `list`; run it only from `add` and startup (or a `notes.py tidy` verb). Fixes §3 bug 3 and the "where did my note go" confusion. **S**
4. **Backfill `captured_at` from transcript mtime** (`backfill.py:224`). Fixes compass recency inversion. **S**
5. **Write body before index in `add_note`/`add_learning`; use `os.replace` for archive moves; append-to-archive before remove-from-active.** Makes every crash state something `repair` recovers rather than deletes. **S/M**
6. **Hold the FileLock across read-modify-write** in `update_note`, `archive_note`, `unarchive_note`, `archive_learning`, `remove_learning` (one `_locked_index(year_dir)` context manager). Fixes §3 bug 4. **M**
7. **Fix the two broken skill paths**: `/notes learning` → `learnings`; `/review-learnings` Steps 4–5 → a `notes.py mark-reviewed` verb invoked via the launcher. **S**
8. **Collapse the resolvers**: delete the four `_git_repo_root` copies and per-tool `*_dir` fallbacks; every tool calls `core.utils.state.resolve_target_state_dir()` / `state_dir_from_env()`. Also stop `ScribeStore.__init__` from calling `ensure_layout` (do it lazily in `add_*`). **M**
9. **Merge researcher + captures stores and CLI helpers** into one sidecar-store module; make `/captures` a skill; put one frontmatter module under `core/` and migrate scribe learnings to it. **M/L**
10. **Split `scribe/notes.py`**: `scribe/policy.py` (auto-archive rules), `scribe/maintenance.py` (repair, backfill-brief, backup+restore, mark-reviewed), `scribe/infer.py` (claude tagging, with `--no-infer` default for `/wrapup`), delete `migrate`/`handoff-sessions`/`_repo_scribe_dir`/template gate unless it is wired in, fix the docstrings; then update `scribe/CLAUDE.md`, README, `cli-tools.md`, `cli-index.md` to the real archive policy, real layout, and real subcommand list. **L**

Honourable mentions: schedule `backup_indexes` and `observations.py archive --apply` in `cron_registry`; delete `refiner/round_counter.py` in favour of harden's; add tests for `synthesize`/`backfill`/`cmd_learn`; make `/wrapup` and `/refine` save long bodies via `--content-file`.
