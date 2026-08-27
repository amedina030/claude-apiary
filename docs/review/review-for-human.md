---
type: architecture
title: "Deep Review 2026-08 (plain-language edition)"
scope: project
description: The same repo-wide assessment as review-for-llm.md, written for a person — what's good, what's broken, what to keep, what to drop, and what to decide
framework_version: "1.0"
last_verified: 2026-08-26
---

> **Snapshot of 2026-08-26; superseded by the remediation — see CHANGELOG. Deleted at close-out (T-2026-271).**

# claude-apiary — Deep Review (plain-language edition)

**Date:** 2026-08-26, reviewed at `master` @ `1bee5e5`. Nothing was changed.
**Status (end of 2026-08-26):** we went through every section together; the decisions and the changes they made to the plan are in the last section. Secret scanning is already fixed and merged.
**How this was done:** I read the whole repo — every non-test Python file, the frontend JavaScript, every slash-command and agent prompt, all the docs — plus the on-disk state (notes, logs, registry) to see what is actually *used*, not just what exists. Six parallel reviewers each took one area and wrote a detailed report (`docs/review/subsystems/`); I did the cross-cutting analysis myself and spot-checked every headline claim by hand before writing this. The detailed, line-numbered version for an LLM to act on is `docs/review/review-for-llm.md`.

---

## The short version

You built something genuinely useful: **scribe** — notes, learnings, and handoffs that survive between Claude sessions — is the heart of this repo, it's used every day (262 todos, 266 handoffs, 142 learnings so far), and its storage code is well thought out. The per-repo install system is solid and well-tested. Several individual modules are excellent.

But the repo has grown to roughly **twice the size its proven value justifies**, and a few things that are supposed to keep you safe are quietly broken:

1. **Your permission prompts are being suppressed.** Every apiary hook tells Claude Code "allow this tool call" on every call, in every one of your 15 repos. That's a one-line helper (`core/hook_context.py`) added in April to silence a cosmetic error message. In default permission mode, the ordinary "run this command?" prompt no longer appears for anything the hooks touch. Built-in protections (like refusing to delete your home directory) still apply; your own approval doesn't.
2. **The GUI's permission gate fails open.** If its little local server can't start, it approves everything without telling you.
3. **The secret scanner misses the most common leak.** A line like `aws_secret_access_key = …` sails through because of a regex mistake, and if `git` itself errors the scanner says "all clear" instead of "I couldn't check."

And two big pieces of the codebase are not pulling their weight:

- **Runner** (the overnight autonomous pipeline) is a third of all the code and hasn't successfully run since mid-April. Its current default configuration *can't* complete a run — stage 4 writes a file that stage 5 rejects. Everything added to it since April has never been exercised end to end.
- **The GUI** is another quarter of the code, works only on Windows, depends on a dozen undocumented Claude Code internals, and most of its 93 commits are chasing changes in Claude Code's terminal UI.

The biggest single source of mess is the **half-finished migration** from May. The docs say "apiary no longer writes anything to `~/.claude`", but it writes there every session (there are 4,169 stray files in there right now). Three slash commands (`/budgeter-log`, `/budgeter-warn`, `/budgeter-session-warn`) have silently done nothing since May because they toggle a file at the old location. Over 60 statements in the docs describe the old model. The doc-checker passed green the whole time because it only checks that files have the right header fields, not that what they say is true.

**What to do, in order:** fix the safety issues (a couple of days), delete the dead weight (a couple of days, ~6–8k lines), merge the eight copies of everything (a few weeks), add basic engineering plumbing — CI, a linter — (a couple of days), regenerate the docs, and then make three strategic calls only you can make (runner, GUI, compass). Don't add features until the consolidation is done.

---

## What's genuinely good

- **Scribe's storage design** — one index file per note type per year, one markdown file per note, atomic writes, file locks. Right instincts. Worth investing in.
- **The per-repo install** (`apiary install`, the registry, profiles) — tested against real git repos, idempotent, careful about not clobbering your `CLAUDE.md`.
- **Runner's "validate → reject → retry" loop** — the LLM produces JSON, deterministic code rejects it with reasons, the LLM tries again. This is the right way to make LLM pipelines debuggable. The per-step executor and the plan validator are the best-engineered code in the repo.
- **Harden's validator layer** — it keeps IDs out of the model's hands and enforces "every finding is adjudicated exactly once." Real value, not ceremony.
- **The GUI's core decisions** — rendering from the transcript file rather than scraping the terminal, keeping the file manifest on the Python side, using the structured question format instead of parsing menus. Good judgement, even if the codebase around it sprawled.
- **1,696 tests that pass**, mostly hermetic (they don't touch your real data). That's a real asset most personal projects never have.
- **The comment trail** — lots of code cites the ticket or the attack finding that motivated it. That paper trail is unusual and valuable.

---

## The problems, plainly

### A. Safety (fix first)

| What | Why it matters | Where |
|---|---|---|
| Every hook says "allow" | Suppresses your normal approval prompts in all 15 repos | `core/hook_context.py:41-46` |
| Push gates say `"block"` | Not a real value in Claude Code's hook vocabulary (`allow`/`deny`/`ask` are); works by luck today | same file |
| GUI permission server fails open | If its local port can't bind, it rubber-stamps every tool call silently | `gui/permission_mcp.py:204-206` |
| Secret scanner misses `aws_secret_access_key`, `github_pat_…`, Stripe keys, JWTs, passwords with punctuation | The gate exists to stop exactly these | `scripts/secret_scan.py:109-113` |
| Secret scanner says "clean" if `git` fails | A security check that fails quietly is worse than none | `scripts/secret_scan.py:243-259` |
| Secret scanner prints the full secret to the terminal | It "redacts" by truncating at 100 characters; every real key is shorter | `scripts/secret_scan.py:224-232` |
| Runner's approval stage can `git push` without asking | Only in interactive mode, but that's when you're least expecting it | `runner/approval.py:347-363` |
| Runner's `claude` subprocess inherits all your permissions | No `--disallowedTools` or turn limit is passed | `runner/claude_subprocess.py:142-144` |
| GUI can send raw Ctrl+C to Claude | Your own rule says it must never; it's enforced only by a code comment | `gui/app.py:83-85` |

### B. Things that are broken and nobody noticed

- **`/budgeter-log`, `/budgeter-warn`, `/budgeter-session-warn`** — toggle a file at the old `~/.claude` location; the hooks read the new one. Dead since 2026-05-05.
- **Both scheduled jobs** (nightly runner, weekly compass synthesis) — `cron_health check` says "broken: command drift." Your personality profile hasn't been re-synthesized since April 17.
- **Claude can't push from this repo right now** — the doc-vs-CLI checker fails on one line (a subcommand documented in the wrong table shape), and the push hook blocks on that.
- **`apiary doctor pointers --fix`** — documented in four places, fails with "unrecognized arguments". The `doctor` command never learned the `--fix` flag.
- **The runner's default mode can't finish** — stage 4 (monolithic executor) doesn't write a `schema_version`; stage 5 refuses files without one.
- **Marking an archived note done/updated prints "done" and does nothing.** Combined with "done notes get archived one day after they were *created*", the sequence `done → list → update` silently loses the update.
- **`/research verify` corrupts entries** whose title or URLs contain `:` or `#` — a little worse every time you run it. Two entries are already damaged.
- **The GUI drops messages** that arrive in a small window while it attaches to a transcript (you see them after a reload).
- **Closing a GUI tab may not kill Claude.** The wrapper terminates `cmd.exe`, not the `node` process underneath; closed tabs can keep burning quota.
- **Budgeter's numbers are off.** It triple-counts messages that have thinking + text + a tool call, adds 25% phantom entries from parallel tool calls, and ignores cache-creation tokens (the most expensive kind). Its "this task looks expensive" warning is right 9% of the time. Its `[CONT]` task-chaining feature is dead because the baseline file is deleted every turn.
- **Compass's backfill** stamps old sessions with today's date, so old behaviour outranks recent behaviour in the profile.

### C. Dead weight (safe to delete, all verified to have no callers)

About 6–8k lines. Highlights: the whole "mailbox" mechanism in core (it exists so a hook doesn't need a lock the hook already takes); three hooks that run on every tool call and can never do anything (`check_install`, `check_install_stop`, `startup_hook`); `core/config.py` (zero callers, still recommended in the style guide); budgeter's estimator + tuner + feedback log; runner's `run_tracker` (unreachable), `usher_order` (no CLI, no producer), and a dozen dead helpers; scribe's template gate and migration stubs; a duplicate `round_counter.py`; four legacy scripts from the migration (`bootstrap.py`, `uninstall_hooks.py`, `install_context_rules.py`, `audit_portability.py`); the `setup.py` stub; a 5 MB `.apiary.pre-migration/` folder; 12 remote branches already merged.

### D. Sprawl — eight copies of everything

Verified counts: 8 functions that find the git root (4 of them byte-identical), 6 that find the state directory, 5 JSON readers, 7 hand-rolled "atomic write" helpers, 3 frontmatter parsers that can't read each other's files, 5 "strip the code fence off Claude's JSON" functions in runner alone, 5 `run_claude` wrappers, 6 UUID-safety guards, 3 `slugify`s. The researcher and captures tools are 73% identical files. `scribe/notes.py` is 1,643 lines; `gui/web/app.js` is 3,293 lines in one closure with five permanent timers; the runner has three functions over 280 lines (the biggest is 471).

### E. Speed

Every apiary hook costs about 150 ms because the launcher starts a second Python just to run the script. A single `Bash` tool call triggers 11 hooks ≈ **1.7 seconds of pure overhead**, and about half of those hooks do nothing. Over a long session that's minutes of waiting. One dispatcher process per event instead of one process per hook would cut this ~8×.

### F. Docs

Over 60 statements are stale. The single stalest file (`docs/reference/cli-index.md`, last verified April 23, still lists the deleted `setup.py --global`) is the one injected into every session's context. The README's directory map is ~40% wrong. `MIGRATION-PLAN.md` was deleted but 15 files cite it. The docs framework (`docs/check.py`) is ceremony — it checks headers, not content. The one checker that works (`check_cli_claims.py`) runs only at push time and is currently the thing blocking pushes.

### G. Engineering plumbing

No CI. No linter. No formatter. No type checker. No coverage. The version has been `0.1.0` since day one with no tags; the `apiary update` command the docs describe doesn't exist. The install script for Mac/Linux has never been run (and has a known failure on modern Debian/Homebrew Python). Tests are broad but heavily mocked exactly where the hard bugs live (runner's overnight flow, the GUI's session management), and there's no end-to-end test anywhere — one test driving two real runner stages with a fake `claude` would have caught the "can't finish a run" bug the day it was introduced.

---

## Keep, fix, or drop

| Area | Verdict | One line |
|---|---|---|
| **Scribe** (notes/learnings/handoffs) | **Keep — invest** | The product. Fix the archive bugs, split the giant file, add restore to backup. |
| **Core install/registry/profiles** | **Keep** | Solid. Fix the two "clobbers your settings" bugs, finish the migration. |
| Core drift/mailbox/cascade | Keep drift + cascade; **drop mailbox** | ~900 lines for a problem the hook can solve in place. |
| Core hooks | **Consolidate into one dispatcher**; delete 3 dead ones | The 1.7 s tax. |
| **Budgeter logger + session-length nudge** | **Keep — fix the math** | Useful once the counting is right. |
| Budgeter cost warnings + tuner + `[CONT]` | **Drop** | 9% precision; the signal it needs isn't visible when it fires. |
| **Harden** validators + agent prompts | **Keep** | Real value. |
| Harden's 746-line skill | **Move the logic to Python** | ~10k tokens per run, and prose is the least reliable place for control flow. |
| **Runner** | **Decide** (see below) | Best ideas in the repo, heaviest dead weight, hasn't run in 4 months. |
| **GUI** | **Keep with a cost ceiling** | Personal tool; fix the 3 serious bugs; stop chasing the TUI. |
| **Compass** | **Label as experiment or drop** | Unmeasured, stale, cron broken. |
| Researcher + captures | **Merge** | Same tool twice; give captures a skill. |
| Refiner | Keep; delete its duplicate round counter | Fine. |
| Incubator | **Keep** | Sound; two small fixes. |
| Docs framework (`check.py`) | **Shrink** | Keep index check + "is this older than its last edit"; drop the ceremony. |
| `check_cli_claims.py` | **Keep, promote to pre-commit** | The one checker that catches real drift. |
| Migration leftovers | **Delete** | ~2,700 lines and 15 dangling references. |

---

## Suggested order of work

1. **Safety (≈2 days).** Stop hooks from voting on permissions (and verify prompts come back). Make the GUI permission server fail closed. Fix the secret scanner's regex, fail-closed, and redaction. Wrap the budgeter hooks so a corrupt file can't wedge a session. Fix the GUI's message-drop race, Ctrl+C guard, and process teardown. Remove `git push` from runner's approval stage.
2. **Unbreak (≈2 days).** Budgeter toggles → one `/budgeter` command that calls the real flag code. Fix the one doc line blocking pushes. Add `--fix` to `doctor`. Repair the two cron jobs. Scribe: archived-note mutations, done-by-completion-date, `/notes learning`, `/review-learnings`. Fix the researcher parser. Give the monolithic executor its `schema_version`. Move the `~/.claude` writes to the per-repo location.
3. **Delete (≈1–2 days).** Everything in section C, plus the merged branches and the README directory map.
4. **Consolidate (≈2–3 weeks).** One hook dispatcher. One set of shared helpers (git root, JSON I/O, state dir, frontmatter, Claude-envelope parsing). Split `notes.py`, `app.js`, and the runner's giant functions. One branch per runner run. Merge researcher/captures. Harden orchestration into Python.
5. **Plumbing (≈2 days).** CI on three OSes. `ruff`. Modern `pyproject`. Coverage. Wire up the JS tests. One end-to-end runner test. Decide the versioning story.
6. **Docs (≈2 days).** Generate the CLI reference from the code. Sweep the 60 stale statements. Shrink the doc checker.

---

## Decisions — made together on 2026-08-26

We walked through every section above. Here is what was decided, and what it changed in the plan.

1. **Permissions:** fix the hook so it stops saying "allow", and check that prompts come back. You value autonomy and run in auto mode; that's unchanged. If prompting ever gets annoying, we add explicit allow rules the sanctioned way (there's a built-in helper that mines your transcripts for the commands you always approve) — never a hook vote.
2. **The rest of safety:** approved as described — GUI permission server fails closed, runner can't push or sweep loose files, runner's `claude` gets a deny list and a turn cap, Ctrl+C blocked in code.
3. **Unbreak list:** approved.
4. **Dead weight:** approved, with one change — the scribe template gate is not deleted; we keep the useful half (a required-sections check, no hash-ack ceremony) and ship a default template for **every note type**, forward-only. Handoffs, decisions and blockers get required sections; quick note types get guidance only.
5. **Sprawl:** approved, plus prevention so it doesn't come back: one obvious `core/utils`, a hook in every apiary repo that says "that helper already exists at …" the moment a duplicate is written, and a near-duplicate check in CI.
6. **Speed:** one hook dispatcher instead of ten processes. Approved.
7. **Docs:** approved, plus the long-term rule — anything that can drift is generated from code or tested against it; prose stays short; a doc-shaped error files a todo automatically.
8. **Plumbing:** CI, linter, coverage, packaging, faster tests, one end-to-end runner test. Approved.
9. **Runner: revive it, fully.** The reason it went unused is exactly the flakiness this review found. Order: end-to-end test first, then the correctness fixes, then ten consecutive real nightly runs as the bar for "works as intended." No new features until it clears that bar.
10. **Compass: keep it, fix it, and measure it.** Three instruments so "is it working?" has an answer: an offline test of whether the profile predicts your replies better than no profile; a live A/B (profile on/off by session) counting clarifying questions and pushbacks; and health/staleness in `doctor`. Decide keep/remove on the numbers.
11. **Versioning:** build `apiary update` and start tagging.
12. **Stray `~/.claude` files:** deleted (4,027 files; anything from the last day was kept).

Already done: the secret-scanning hardening is merged (PR #31), and the doc drift that was blocking pushes is cleared.

---

## How confident am I?

High on the facts: every bug listed here was verified by reading the code, running a command, or reproducing it in a temp directory, and I re-checked each headline claim from the six sub-reviews myself before including it. Line numbers are as of `1bee5e5`. The one claim that deserves an empirical check before you act on it is #1 in Safety — the docs are clear that a hook `allow` approves anything not covered by an explicit `deny`/`ask` rule, and you have none, but the cleanest proof is to remove the `allow` in a test repo and watch a prompt come back.

Medium on the effort estimates — they assume an LLM executor working from `review-for-llm.md` with you reviewing PRs.

The strategic recommendations (runner, GUI, compass) are judgement calls; the evidence for them is in the usage table in the LLM edition, §1.
