---
type: architecture
title: "Deep Review 2026-08 (LLM edition)"
scope: project
description: Repo-wide engineering assessment of claude-apiary for an LLM executor — verified findings with file:line evidence, keep/cut verdicts, and a phased remediation plan
framework_version: "1.0"
last_verified: 2026-08-26
---

> **Snapshot of 2026-08-26; superseded by the remediation — see CHANGELOG. Deleted at close-out (T-2026-271).**

# claude-apiary — Deep Review (LLM edition)

**Audience:** an LLM (Opus / Fable class) that will plan and execute the remediation. This document is the map; the six appendices under `docs/review/subsystems/` are the territory — read the relevant appendix before touching a subsystem.
**Reviewed at:** `master` @ `1bee5e5`, 2026-08-26. Read-only; nothing was changed. Every claim below was verified by reading code, running commands, or inspecting on-disk state; each carries a `path:line` or a command you can re-run.
**Companion:** `docs/review/review-for-human.md` (same conclusions, plain language).
**Status (2026-08-26, end of day):** the owner walked through every section and settled the open decisions — see **§5a** (amendments, which override conflicting text above) and **§6** (decision record). Phase 0.3 (secret scanning) is merged (PR #31). This document is a dated snapshot; the plan in §5 + §5a is the live part.

---

## 0. How to use this document

1. Read §1 (facts) and §2 (verdict) fully.
2. Treat §3 (cross-cutting) as the constraints that apply to *every* change you make.
3. §4 gives per-subsystem verdicts and the top bugs; the appendix for that subsystem has the full bug list, the dead-code inventory (all grep-verified), the doc-drift table, and a ranked top-10.
4. §5 is the phased plan. Execute phases in order; Phase 0 and Phase 1 are small and safety-critical. Do not start Phase 6 items without the human's decision (§6).
5. Conventions for executing are in §7. In particular: **branch per change, PR to master, run `poetry run pytest -q` and `python docs/check.py` and `python docs/check_cli_claims.py` before every push.**

Finding IDs: `C-n` = critical/high, `X-n` = cross-cutting, per-subsystem IDs are in the appendices (e.g. runner "Bug 1", core "Bug 4", budgeter "B0").

---

## 1. Repo snapshot (facts)

| Metric | Value | Source |
|---|---|---|
| Tracked files | 370 | `git ls-files` |
| Python | 261 files, ~57k lines (`3,326` functions) | AST scan |
| JS/CSS/HTML | ~6.9k lines (`gui/web/app.js` 3,293; `app.css` 2,166) | `wc -l` |
| Skill/agent prompts | 20 files, 2,181 lines, ≈26k tokens (`harden.md` alone ≈9.3k) | `wc`, bytes/4 |
| Tests | 98 files, ~22k lines; **1,696 pass, 2 skip, ~2.5 min** | `poetry run pytest -q` |
| Commits | 516, single author, 2026-03-14 → 2026-08-25; 370 of them in April | `git log` |
| Type hints / docstrings | 39% of functions annotated, 26% docstring'd | AST scan |
| Functions >100 lines | 20 (largest: `runner/run.py:471 _run_detached_impl` 471 lines, `runner/executor.py:808 main` 379, `runner/run.py:1232 main` 286, `budgeter/hooks/pre_tool_use.py:43 main` 246) | AST scan |
| `except Exception` / `except …: pass` | 75 / 37 (hotspots: `gui/session.py` 9, `gui/app.py` 8, `runner/run.py` 6) | grep |
| `sys.path.insert` hacks | 87 files | grep |
| CI / lint / format / types / coverage | **none** (no `.github/`, no ruff/black/mypy config, no pytest-cov) | tree |
| `apiary doctor` | all 8 checks green | run |
| Registered repos | 15 in `.repos/registry.json` | on disk |

**Size by subsystem (lines, tracked):** runner 18.3k · gui 15.5k · core 10.8k · scribe 5.7k · scripts 4.6k · docs 4.4k · budgeter 3.5k · harden 2.9k · compass 1.3k · incubator 1.1k · researcher 1.0k · captures 0.9k · refiner 0.4k.

**Usage evidence (what is actually exercised):**

| Tool | Evidence | Read |
|---|---|---|
| scribe | 262 todos, 266 handoffs, 142 learnings, 51 decisions, 55 context notes in `.repos/claude-apiary-1/scribe/` | **The workhorse.** Used every session. |
| budgeter (logging) | 886 sessions, 26,507 log entries, 70 MB `budgeter/data/usage_log.jsonl`, 2026-04-02 → today | Heavily used as a *logger*. |
| budgeter (warnings) | 53 warnings fired in 3,764 task turns; measured precision 5/53 = 9% (`report.py --feedback`) | Feature has not earned its keep. |
| runner | Last real run commits 2026-04-06 → 04-13; last ticket touched 2026-04-23; `.repos/claude-apiary-1/runner/` empty; no `run_history.jsonl`/`overnight.jsonl` anywhere; 0 live `runner/*` branches | **Dormant 4+ months.** Everything landed since (schema versions, monolithic executor, crash locks, cron_health, multi-repo) has never run end-to-end. |
| compass | 71 observations; `personality.md` "synthesized from 7 sessions, last updated 2026-04-17" | Weekly synthesis has not run in 4 months. |
| cron | `python -m runner.cron_health check` → `overnight-runner broken (command drift)`, `compass-weekly-synthesis broken (command drift)` | **Both scheduled jobs are broken.** |
| GUI | 93 commits in 4 months, most chasing Claude Code TUI behaviour; packaged build exists | Actively maintained, single platform. |
| harden / refine / incubator | Skills exist; `harden/tmp/` has round files from Apr–Jun; incubator spawned the 13 side repos | Used, moderately. |
| researcher / captures | A handful of entries; captures has no skill at all | Barely surfaced. |

**Hook overhead (measured, synthetic payload):** each hook ≈140–360 ms because the per-repo launcher spawns a *second* Python for the script (`.claude/apiary/launch.py:107-110`). A `Bash` tool call fires **9 PreToolUse + 2 PostToolUse hooks ≈ 1.7 s**; `Read` ≈ 1.2 s; every assistant turn ends with 3 Stop hooks ≈ 0.8 s. That is ~18 interpreter starts per Bash call, roughly half of them no-ops (see X-1).

---

## 2. Executive verdict

**The core idea is good and the core tool (scribe) is earning its place.** Cross-session notes/learnings/handoffs injected at startup is the feature that makes this toolkit valuable, and the on-disk store (`scribe/store.py`) shows sound instincts (atomic index writes, file locks, typed-year layout). The per-repo install model (`core/install.py` + registry + profiles) is well-tested and mostly right. Several modules are exemplary (`core/apiary_profiles.py`, `core/utils/jsonc.py`, `runner/executor.py` + `validate_plan.py`, `harden/validate_*.py`, `gui/file_refs.py`, `gui/ask_prompt.py`, `docs/check_cli_claims.py`).

**But the repo is carrying roughly 2× the code its proven value justifies, and three of its safety mechanisms are broken in ways that matter:**

1. **Every PreToolUse hook auto-approves the tool call** (`core/hook_context.py:41-46` hard-codes `permissionDecision: "allow"`; 10 hooks call it on every call in every bootstrapped repo). Per the Claude Code docs, a hook `allow` approves any call not matched by an explicit `deny`/`ask` rule or a built-in protection, and this repo ships zero `deny`/`ask` rules. In default permission mode the ordinary "run this command?" prompt is therefore suppressed in all 15 repos. Introduced by `00bce9d` (2026-04-03) to silence a cosmetic hook error. The two push gates emit `"block"`, which is not a documented value (`allow|deny|ask`).
2. **The GUI's permission gate fails open** (`gui/permission_mcp.py:204-206`): if the loopback bridge fails to bind, the MCP server approves everything silently, and the flag that tells `claude` to use it is set *before* the bind is attempted (`gui/app.py:517-523`).
3. **The commit-time secret scanner misses the most-leaked credential shape** (`aws_secret_access_key = …` — `\b` cannot fire inside a `_`-joined key, `scripts/secret_scan.py:109-110`), fails open if `git` errors (`:243-259`), prints the full secret to stderr (`_redact` truncates at 100 chars, `:224-232`), and its entropy gate is mathematically unreachable for hex values ≤ 16 chars (`core/hooks/pre_push_secret_scan.py:77-79`).

**And two subsystems are strategically over-weight:** runner (32% of LOC) has been dormant since April and its *default* configuration cannot complete a run (C-3); the GUI (27% of LOC) is a Windows-only wrapper coupled to 12 undocumented Claude Code internals with no CI build.

**Migration debt is the single largest source of drift.** The 2026-05 move from a global `~/.claude` install to per-repo was declared done, but a second generation of code still runs on every session: identity/flag/history files are written to `~/.claude` (780 identity files + 3,389 tmp files today — the docs say "apiary writes nothing there"), three slash commands toggle files at the old path (silently broken since 2026-05-05), and 60+ doc statements describe the old model. The doc-conformance gate (`docs/check.py`) passed green throughout because it checks frontmatter shape, not content.

**Net recommendation:** fix the safety issues (days), delete the dead weight (days, ~6–8k lines), consolidate the eight copies of everything (weeks), add CI/lint (days), regenerate the docs from introspection, and then make three strategic decisions the human must own (runner, GUI, compass — §6). Do not add features until Phase 3 is done.

---

## 3. Cross-cutting findings (constraints on every change)

### X-1 · Hook architecture: fan-out, double-spawn, and dead hooks
- 15 PreToolUse/PostToolUse/Stop registrations in `.claude/settings.json`, generated by `core/hooks_factory.py`. `learnings_inject_hook` is registered **three times** (`hooks_factory.py:90-92`). `check_install.py` reads a manifest nothing writes (`core/hooks/check_install.py:24-25`; CHANGELOG records its removal) and its advice is `python setup.py --global`, a stub that exits 1. `check_install_stop.py` is a documented no-op (`:17-18`) still registered (`hooks_factory.py:116`). `startup_hook.py` inspects `~/.claude/CLAUDE.md` for a zone the per-repo model writes to `<repo>/CLAUDE.md` (`startup_hook.py:20` vs `core/install.py:328`) — it always yields nothing.
- The launcher runs `subprocess.run([sys.executable, script])` (`core/launcher_template.py:110-113`): two interpreters per hook.
- **Recommendation (high value):** one dispatcher per event (`core/hooks/dispatch.py pre|post|stop|prompt`) that imports the hook modules and runs them in-process, registered once per event with an empty matcher; and make the launcher `runpy.run_path` the target instead of re-spawning. Expected: ~18 → 1–2 interpreter starts per tool call (≈1.7 s → ≈0.2 s). Fail-open semantics must be preserved per hook (wrap each in try/except, log to a file, never raise).
- Every hook response must stop voting on permissions: `hook_allow` should omit `permissionDecision` unless a decision is intended; gates should use `deny` (or exit 2), not `block`. Verify empirically in a bootstrapped repo in default permission mode before and after.

### X-2 · Two install generations coexist (the migration is not finished)
Live code still on the global model: `core/session.py:11,80-84` (identity + flag files in `~/.claude`), `core/startup.py:28,95`, `core/hooks/save_transcript.py:22-24,63,89` (`~/.claude/.session-history.json`, rewritten every turn), `core/hooks/startup_hook.py`, `core/hooks/check_install.py`, `hooks_lib.hook_cmd`'s `$HOME/.claude/apiary_launch.py` branch (`core/hooks_lib.py:109-133`), `scripts/bootstrap.py`, `scripts/uninstall_hooks.py:37`, `scripts/install_context_rules.py:44`, `budgeter/commands/budgeter-{log,warn,session-warn}.md:11`, `profiles/base.jsonc` permission globs (`*/.claude/.session-history.json`, `core/config/session-registry.json`). The documented per-target `sessions/{history.json,identity-*.json,transcripts/}` (`docs/architecture/per-repo-install.md:92-95`) has no live writer. `MIGRATION-PLAN.md` was deleted (`f1220d8`) but is cited from 15 files. **Rule:** finish the migration before building anything on top of session identity.

### X-3 · Duplication (grep-verified counts)
git-root resolvers ×8 (`core/utils/state.py:69`, `core/flags.py:30`, `core/git_hooks.py:38`, inline in `pre_push_doc_conformer.py:133`, `scribe/notes.py:119`, `compass/store.py:51`, `researcher/store.py:43`, `captures/store.py:51` — the last four byte-identical) · state-dir resolvers ×6 · JSON-object readers ×5 · atomic tmp+replace writers ×7 (and `hooks_lib.save_settings`/`config.write_config` are *not* atomic) · frontmatter/YAML parsers ×3 with mutually incompatible dialects (`scribe/store.py:84-131`, `researcher/_yaml_mini.py`, `docs/check.py:47`) · claude-envelope/fence strippers ×5 in runner + ×3 in scribe/compass · `run_claude` wrappers ×5 in runner · UUID path-traversal guards ×6 · `slugify` ×3 · `round_counter.py` ×2 (`refiner/` is a strict subset of `harden/`) · note-prefix tables ×3 · `researcher/store.py` vs `captures/store.py` 73% identical. **Rule:** when you touch one copy, replace all copies with a `core/utils/` helper in the same PR; do not add a ninth.

### X-4 · Error handling is inconsistent in both directions
Hooks that are supposed to fail open crash instead: `budgeter/hooks/pre_tool_use.py:43-288` has no try/except (a truncated baseline JSON wedges every subsequent tool call in that session — reproduced), same for `post_tool_use.py:67`, `core/hooks/save_transcript.py:66-92`. Meanwhile 37 `except: pass` sites hide configuration errors from the user (`gui/scribe_aggregator.py:229`, `gui/session.py:221`, `gui/pty_wrapper.py:188` turns a read error into "Claude exited"). Baseline files are written non-atomically (`budgeter/lib/logger.py:412`). **Rule:** hooks wrap `main()` in try/except and log to a file; CLIs raise with a message; never `pass` on an exception that a user needs to know about.

### X-5 · Docs are the least reliable artifact in the repo
60+ stale statements across 20 files (exhaustive list: appendix `infra-docs-skills.md` §4). ~25 describe `~/.claude` as live state, 12 name `setup.py` as the installer, 10 cite the deleted `MIGRATION-PLAN.md`. `README.md`'s Repository Structure section is ~40% wrong. **The stalest doc, `docs/reference/cli-index.md` (`last_verified: 2026-04-23`), is injected into every session** by `core/hooks/startup_prompt_hook.py:212-218` and still lists `setup.py --global`. `docs/check.py` cannot catch any of this (it validates frontmatter shape; `KNOWN_TOOLS = {"budgeter","scribe","core"}` at `:44`). `docs/check_cli_claims.py` is the one checker that works — and it currently **fails at HEAD** (`incubator/cli.py verify` documented as its own section, `cli-tools.md:292`), which means `core/hooks/pre_push_doc_conformer.py` blocks every Claude-driven `git push` from this repo right now. **Rule:** regenerate reference docs from introspection where possible; delete prose that duplicates code; a doc claim you cannot verify by reading code is a bug.

### X-6 · Tests: broad, hermetic, but mocked where it matters
1,696 passing tests is genuinely good, and isolation is careful (`APIARY_BUDGETER_TEST_ISOLATION`, `HOME`+`USERPROFILE` redirection, tempdirs everywhere). Gaps: `runner/test_run_detached.py` mocks worktree creation, stage execution, and commits in every test (cannot see runner Bugs 1/3/4/10); `runner/approval.py` and `core/cli.py` have zero tests; `gui/app.py`'s `App` class (where the GUI's thread races live) has none; `subagent_tracker.py` (497 lines) has none; `compass/synthesize.py` and `backfill.py` have none; JS tests exist for 3 modules but are wired to nothing; `runner/test_orchestrator.py` writes to the real `.apiary/runner/locks/`; one flaky GUI test (1 failure in 9 runs, wall-clock-based). **There is no end-to-end test anywhere** — a single test driving two real runner stages with a fake `claude` on `PATH` would have caught C-3 the day it was introduced. Suite takes 2.5 min because `core/test_{install,drift,cascade,uninstall}.py` each `git init` real repos (~2 s/test); share a fixture.

### X-7 · Project hygiene
No CI, lint, format, type-check, or coverage. `VERSION`/`pyproject` say `0.1.0`, never changed, no tags; `apiary update` (referenced from CHANGELOG, migrations/README, per-repo-install.md, state.py) **does not exist** (`core/cli.py:141-184`); `version.json` is written and never read. `pyproject.toml` uses the deprecated `[tool.poetry]` metadata (6 warnings from `poetry check`) and its `packages` list omits `captures, compass, researcher, incubator` — tests pass only because of `--import-mode=importlib`. `.gitignore` has duplicate lines (37–38) and a bare `.claude/` appended by `apiary install`. No `* text=auto` in `.gitattributes` (234 files `i/lf w/crlf`). All 12 remote branches are already merged into master. `code-style.md:72` says "Use unittest. No pytest." while pytest is the runner (fine in practice; fix the standard). Migration leftovers ≈2,700 lines (`scripts/bootstrap.py`, `uninstall_hooks.py`, `install_context_rules.py`, `audit_portability.py`, `setup.py`, `runner/cron_setup.md`, `.apiary.pre-migration/` 5 MB).

### X-8 · Skills are programs written in prose, and three are broken
16 slash commands ≈23k tokens. `harden/commands/harden.md` (746 lines, ≈9.3k tokens) is a full orchestrator in prose — path selection, cost formula, retry/degrade, budget abort, worktree lifecycle — that `runner/auto_harden.py` already implements 80% of in Python. `/budgeter-log|warn|session-warn` toggle `~/.claude/<flag>-enabled` while `core/flags.py:23` reads `<repo>/.claude/apiary/flags/` — silently dead since 2026-05-05. `/notes learning` is an argparse error (`--type learning` is not a valid choice). `/review-learnings` steps 4–5 bypass the launcher and stamp `last_review` in the wrong directory, so the "run /review-learnings" nudge never clears. `/runner-prep` writes intake JSON to `runner/intake/`, a directory the runner no longer reads. Four budgeter skills lack frontmatter. **Rule:** a skill invokes CLIs via the launcher idiom and never embeds logic the CLI could own; orchestration belongs in Python.

### X-9 · Strategic weight vs proven value
| Subsystem | Share of LOC | Proven value | Assessment |
|---|---|---|---|
| runner | 32% | Worked in April; dormant since; default config cannot finish (C-3) | Over-weight. Decide: shrink to executor+validators, or revive with an e2e test first. |
| gui | 27% | Used daily by one person on one OS | A sink competing with the toolkit; cap its cost. |
| core | 19% | Install/registry proven; drift/mailbox/cascade ~900 lines for a problem the hook could solve inline | Delete mailbox; keep the rest. |
| scribe | 10% | The product | Invest here. |
| budgeter | 6% | Logging valuable; warning feature 9% precision; CONT chaining dead | Keep logger + session nudge; delete estimator/tuner. |
| compass | 2% | Unmeasured; profile stale 4 months; cron broken | Label as experiment or remove. |

---

## 4. Per-subsystem verdicts and top bugs

Each entry: what it is → verdict → the bugs that matter most (IDs refer to the appendix) → keep/cut table. Full detail in `docs/review/subsystems/<name>.md`.

### 4.1 core/ (appendix: `subsystems/core.md`)
Install/registry/pin-file model, drift/mailbox/cascade/doctor, 13 hooks, shared utils. **Verdict: keep the install layer; delete mailbox + dead hooks; finish the migration.**
- **Bug 1** `apiary doctor <check> --fix` unreachable: `core/cli.py:162-170` never defines `--fix` (verified: argparse error). Documented in 4 places. Zero tests for `cli.py`.
- **Bug 2** Mailbox is never drained automatically — only `apiary mailbox`/`doctor mailbox --fix` call `process_pending`; docs say "on session open". **Bug 3** `process_pending` can delete messages without applying them (`KeyError` mid-loop, `core/mailbox.py:147,158-161`).
- **Bug 4** Re-install after registry loss leaves `self-pointer.uid` ≠ registry uid, silently rerouting scribe state to `<repo>/.apiary/scribe/`; no doctor check compares pins to registry.
- **Bug 7** `_apply_profile_permissions` (`core/install.py:277-283`) overwrites user-owned `settings.json` keys on every re-install; `_APIARY_OWNED_KEYS` is defined and never used. **Bug 8** `hooks_lib.is_apiary_entry` (`:48-56`) deletes any user hook whose command contains `/runner/`, `/scribe/`, etc.
- **Bug 9** Drift check rewrites `self-pointer.json` on *every tool call* (no once-per-session guard), racing with the launcher's read on Windows.
- **S1** Writes to `~/.claude` every session (X-2). **S2** `pre_push_doc_conformer.py:146-155` executes `<pushed-repo>/docs/check_cli_claims.py` — repo-provided code — at push time. **S3** secret-scan gaps (C-…: see §2 item 3 and appendix §4).
- Dead: `core/config.py` (0 callers; still recommended by code-style.md), `core/transcript.py`, `core/hooks/extract_transcript.py`, `core/targets.py` (overlapped by doctor), `launcher_template.render`, `check_install*.py`, `startup_hook.py`.

| Component | Verdict |
|---|---|
| `install.py`, `self_bootstrap.py`, `apiary_profiles.py`, `utils/jsonc.py`, `git_hooks.py`, `context_rules.py`, `secret_patterns.py`, `flags.py`, `utils/filelock.py` | keep (fix Bugs 4/7 in install) |
| `cli.py`, `doctor.py`, `drift.py`, `cascade.py`, `hooks_lib.py`, `hooks_factory.py`, `launcher_template.py`, `startup_prompt_hook.py`, `learnings_inject_hook.py`, push gates, `utils/state.py` | improve |
| `session.py`, `save_transcript.py`, `hook_context.py` | rewrite (per-repo paths; no permission voting) |
| `mailbox.py`, `targets.py`, `config.py`, `transcript.py`, `extract_transcript.py`, `check_install*.py`, `startup_hook.py` | delete |

### 4.2 runner/ (appendix: `subsystems/runner.md`)
Six-stage `claude -p` pipeline with JSON artifacts per stage, detached/cron mode, Windows scheduler. **Verdict: the validate→retry loop and per-step executor are the strongest ideas in the repo; the detached branch/worktree model needs one owner; ~40% of the package is unreachable or duplicated. Strategic decision required (§6).**
- **Bug 1 (C-3)** `runner/config.json:17` defaults `executor.mode: monolithic`; `monolithic_executor.py:375-381` never writes `schema_version`; `auto_harden.py:350-354` asserts it → `SchemaVersionError` → every default-mode run ends `stage_failed:auto_harden`. (Verified: `assert_schema_version` raises on `None`.)
- **Bug 2** `validate_plan.py:29,143-152` resolves paths against *apiary's* checkout, so `--target-repo X` fails plan validation for any file that exists only in X. `detached_lib._git` defaults `cwd=REPO_ROOT` likewise.
- **Bug 3** Detached runs produce two branches (`runner/<slug>-<uuid>` from the worktree, `runner/<uuid>` from the executor's `checkout -b`); `queue.py` joins on the wrong one → every morning-review row is `unknown`; each failed run consumes two `max_unreviewed` slots.
- **Bug 4** Preserved worktree on failure → next night `git_setup_failed` *before* `record_attempt` → `max_restarts`/`run_tracker` never trip; `prune_stale_worktrees` scans a directory nothing writes to (`.apiary/runner-worktrees` vs `.runner-worktrees`). **Bug 5** Resume picks the stage guaranteed to fail again (checks `exists()`, not `valid`).
- **Bug 6** `run_lock.is_stale` treats any run >1 h as crashed even with a live PID; `--abort` then removes the worktree of a running job.
- **Bug 7** `text=True` without `encoding=` in ~15 subprocess sites (LLM-authored commit subjects with non-ASCII crash `get_completed_step_numbers`). **Bug 8** Timed-out/failed `claude` calls emit no `<usage>`; cap is blind to them. **Bug 9** Interactive mode mutates the operator's checkout and `approval.py:347-363` **pushes** without confirmation. **Bug 12** `approval.write_scribe_note` bypasses the launcher → notes land in the worktree (deleted on success).
- Security: `claude_subprocess.py:142-144` passes no `--allowedTools/--disallowedTools/--max-turns`; the subprocess inherits the operator's permission grants (this checkout's `settings.local.json` allows `git push *`).
- Dead/duplicated: `run_tracker.py` unreachable, `usher_order.py` no CLI/producer, `stage_review_artifacts` stages nothing, `abort._save_blocker_note` requires the removed global launcher, 5×`run_claude`, 5×JSON salvage, 6×UUID guard, 3×`slugify`, 2×55-line squash-merge block in `approval.py`.

| Component | Verdict |
|---|---|
| `executor.py` (per-step), `validate_intake/spec.py`, `usher.py`, `target_repo.py`, `close_source_todo.py`, `cron_health.py`+`schedulers/` | keep |
| `run.py` core, `validate_plan.py`, `auto_refine/plan/harden.py`, `monolithic_executor.py`, `claude_subprocess.py`, `git_lib.py`, `detached_lib.py`, `run_lock.py`, `abort.py`, `queue.py`, ticket CLIs | improve (see appendix top-10) |
| `run.py` detached branch/worktree flow, `approval.py` | rewrite |
| `run_tracker.py`, `usher_order.py`+json, `run_history` double-write, `cron_setup.md`, stranded `runner/{executions,plans,specs,hardens,reports}/` | delete |

### 4.3 gui/ (appendix: `subsystems/gui.md`)
PyWebView/WebView2 window, pywinpty `claude`, JSONL tail as source of truth, MCP permission bridge, 5 threads per tab. **Verdict: competently built; keep as a personal tool with a fixed cost ceiling; fix the three HIGH bugs; split `app.js` so it can be tested; freeze TUI-scraping features.**
- **#1 HIGH (C-2)** Permission MCP fails open (`gui/permission_mcp.py:204-206`; flag set before bind at `gui/app.py:517-523`); config file left on disk grants blanket approval to any `claude --mcp-config` launched outside the GUI.
- **#2 HIGH** Transcript attach race: `session.py:340-378` reads the file, replays, *then* fast-forwards to `st_size` → records appended in between are never rendered.
- **#3 HIGH** `PtyWrapper.stop` terminates `cmd.exe`, not the `node` grandchild (`pty_wrapper.py:171-172,401-410`; pywinpty `terminate` = `TerminateProcess` on the direct child) → closed tabs keep a `claude` running; `restart_pty` spawns a competitor.
- **#4** "Never send raw Ctrl+C" is enforced only by a JS comment; `send_control` forwards `\x03` (`app.py:83-85`, `app.js:1583-1594`).
- **#5/#6** `App._sessions`/`_active_idx` and the pending-permission dicts are mutated from pywebview per-call threads with no lock; loopback bridge has no auth and parks a thread per request for up to 300 s.
- **#7** `/clear` only detected on the composer path; xterm-typed `/clear` freezes the chat on the old JSONL. **#9** Dead 16 MB ring buffer per tab. **#10** Whole transcript re-read + one giant `evaluate_js` on every tab switch.
- Security: bridge exposes `get_note_body(path)` = arbitrary file read with no allow-list (`app.py:206-210`); XSS audit found **no** hole (all sinks escape or use `textContent`), but the bridge surface makes any future XSS equal to file read + prompt injection + approving rewritten tool inputs. `permission_mcp.log` records every gated tool input (Write contents, Bash commands) in plaintext, unbounded, in `~/.claude`.
- Coupled to 12 undocumented Claude Code internals across 9 files with no adapter boundary (table in appendix §2).

| Component | Verdict |
|---|---|
| `paths/tabs_state/sidebar_state/composer_state.py`, `ask_prompt.py`, `file_refs.py`, `theme.py`, `usage_fetcher.py`, `scribe_aggregator.py`, `picker.py`, `win_*.py` | keep |
| `pty_wrapper.py`, `transcript.py`, `session.py`, `app.py`, `permission_mcp.py`+`permission_bridge.py`, `subagent_tracker.py`, `packaging/` | improve |
| `web/app.js` | rewrite as a split into per-concern modules (after extracting testable pieces) |
| `diag_pty.py`, `_ring`/`buffer`, `poke`, unused bridge methods, legacy `CONFIG_PATH` | delete |

### 4.4 scribe / compass / researcher / captures / refiner (appendix: `subsystems/knowledge.md`)
**Verdict: scribe's store is the right shape and the product's core — fix its mutation/archive bugs and split the 1,643-line `notes.py`; merge researcher+captures; label compass an experiment.**
- **Bug 1** `done`/`drop`/`defer`/`resume`/`update` on an archived note print success and change nothing (`update_note` scans only the active index, `scribe/store.py:590-619`; return value ignored at `notes.py:741-749` etc.). Reproduced.
- **Bug 2** `researcher/_yaml_mini.py` `dumps` quotes values with `:`/`#` but `loads` never unquotes and treats `#` as a comment → every `/research verify` degrades the file further (`'C# generics'` → `'"C'` → `'"\\"C"'`). Two live entries already carry quoted titles. Captures inherits it.
- **Bug 3** "Done" notes auto-archive one day after *creation*, not completion (`notes.py:355-360`); with Bug 1 the sequence `done → list → update` silently loses the update. Auto-archive also runs as a side effect of `list` (a read mutates state).
- **Bug 4** Lost-update race: `update_note`/`archive_note`/… read the index outside the lock, then rewrite under it (`store.py:590/617`, `636/646`); two concurrent `/wrapup`s can drop a row. **Bug 6/7** `add_note` writes index before body; `archive_note` is a 3-step non-transactional move — `repair` deletes rather than recovers after a crash.
- **Bug 5** `compass/backfill.py:224,248` stamps `captured_at = now`, inverting the recency weighting `synthesize.py:50,78` depends on. **Bug 10** every `notes.py learn` without `--tags` spawns a `claude -p` call (10 s budget) on `/wrapup`'s critical path. **Bug 11** `ScribeStore.__init__` does ~45 mkdir/exists calls and is constructed on every Edit/Write/Bash by the learnings hook.
- Data safety: `unlearn` hard-deletes and is still advertised; `update --content` is a non-atomic overwrite (docs say "append-only"); `backup_indexes` snapshots indexes only, has no restore, is unscheduled, and has run once (2026-04-11).
- Dead: `cmd_migrate` stub, `_repo_scribe_dir`, `handoff-sessions`, the whole template-gate subsystem (~150 lines + 12 tests; nothing scaffolds a template and `/wrapup`'s handoff shape contradicts the bundled one), `refiner/round_counter.py`.

| Component | Verdict |
|---|---|
| `scribe/store.py`, `scribe/api.py`, `import_legacy.py`, `compass/store.py`+`observations.py`, `captures/` | keep / improve |
| `scribe/notes.py` | rewrite as a split (policy / maintenance / infer / argparse) |
| `researcher/_yaml_mini.py` | rewrite (or replace with one `core/frontmatter.py`) |
| `researcher/store.py`+`captures/store.py` | merge into one sidecar store |
| `compass/synthesize.py`, `backfill.py` | improve + add tests; product = labelled experiment with a kill switch |
| template gate, legacy int-ID map, `cmd_migrate`, `refiner/round_counter.py` | delete |

### 4.5 budgeter / harden / incubator (appendix: `subsystems/budgeter-harden-incubator.md`)
**Verdict: keep budgeter's logger and session nudge, delete its warning/estimator/CONT subsystem; harden's Python validators are real value, its 746-line skill should move to Python; incubator is sound.**
- **B0 (C-1)** every monitored call auto-approved via `hook_allow` (see §2).
- **B1 HIGH** `pre_tool_use.py:43-288` unguarded; corrupt baseline (non-atomic write, `logger.py:412`) wedges the session's hooks — reproduced. **B2** `get_cumulative_tokens` (`logger.py:341-351`) sums usage per JSONL line, but Claude Code writes one line per content block with identical `usage` → 2–3× over-count (228/384 messages in one real transcript). **B3** parallel tool calls create phantom entries: 6,326 of 25,062 real entries (25%) have `tokens_delta==0, net>0`. **B5** `cache_creation_input_tokens` never counted (4.8M tokens in one session logged as ~1k). **B6** `post_tool_use.py:22,45` reads 64 KB of stdin then silently drops larger Agent payloads — exactly the expensive ones. **B7** `[CONT]` chaining is dead: `stop_session.py:96` deletes the baseline every turn (11 of 25,027 entries chained); the instruction is still injected every session. **B4** toggles broken (X-8). Warning precision 9%; `warn_score_threshold: 1.0` fires on "why"/"explain".
- Harden: validators enforce exactly-once coverage and keep IDs out of the model's hands — keep. `harden.md:533` claims a "parent-session fallback path" for budget that does not exist (`lib/query.py:20`); Defender isolation is by instruction (no `isolation: "worktree"`, `harden.md:488`); worktree/branch never cleaned on Approve/Discard; skill mandates `AskUserQuestion` against user preference.
- Incubator: **B9** `_migrate_spec` passes the whole spec on argv (`cli.py:331`; 32,767-char Windows limit; `--content-file` exists); **B10** recovery hint re-runs `add` → duplicate spec. Templates ship a crashing example (`--since 7d`) and dead `.apiary/` ignores.

| Component | Verdict |
|---|---|
| budgeter logger core, `report.py`, `log_agent_cost.py`, `query_request.py`, `lib/query.py`, session-length nudge | keep / improve (B1, B2, B3, B5, B6) |
| budgeter estimator, `tune.py`, feedback JSONL, `[CONT]`, `budgeter-warn` flag | delete |
| `budgeter/commands/*.md` toggles | rewrite as one `/budgeter <flag>` calling `core/flags.py` |
| harden validators, `assign_ids`, `lenses`, `agents/*.md`, `round_counter.py` | keep |
| `harden/commands/harden.md` | rewrite: extract orchestration to `harden/orchestrate.py` |
| `incubator/` | keep; fix B9/B10 + templates |

### 4.6 scripts / docs / skills / hygiene (appendix: `subsystems/infra-docs-skills.md`)
**Verdict: install scripts are good (POSIX path untested); secret scanner needs its generic rule rewritten; docs framework is ceremony except `check_cli_claims.py`; delete the migration corpse.**
- Secret scanner: `aws_secret_access_key`/`secret_key`/`github_pat_`/`sk_live_`/JWT/passwords-with-punctuation all **miss** (probe table in appendix §3); `_git()` fail-open; `.secretsallow` read from working tree and matched against *lines* too; `_redact` does not redact. The "no gitleaks" argument is valid for Go binaries but not for `detect-secrets` (pure Python) — the real constraint is that git hooks resolve `py -3`/`python3`, not the venv; write that down.
- `docs/check.py`: validates 6 frontmatter keys and whether the strings "budgeter/scribe/core" appear; never bumped `framework_version`; green through a migration that invalidated most docs. `check_cli_claims.py` works — put it in pre-commit, un-skip `apiary`, flag malformed headers.
- `install.sh` uses `pip install --user poetry` (PEP 668 failure on Debian 12+/Homebrew) and has never run (no CI, Windows dev box); `install.ps1` has a dead `-Yes`/`Confirm-Or-Exit`.
- Skills table (name/lines/tokens/launcher compliance/flag accuracy) in appendix §6: 11/16 fully compliant, 3 broken, 2 partial.

| Item | Verdict |
|---|---|
| `install.ps1/.sh`, `update.*`, `preflight.py`, `check_cli_claims.py`, `cli_lookup.py`, `profiles/`, `context-rules/`, `cron_registry/` | keep / improve |
| `secret_scan.py` generic rule, `docs/check.py`, `cli-index.md`, `system-overview.md`, README structure, budgeter toggles, `harden.md` | rewrite |
| `scripts/bootstrap.py`, `uninstall_hooks.py`, `install_context_rules.py`, `audit_portability.py`, `setup.py`, `runner/cron_setup.md`, `.apiary.pre-migration/`, `.apiary/screenshot.png`, root `.claude-session-identity.json` | delete |
| `migrations/` + `doctor versions` + `version.json` | finish (`apiary update`) or delete |

---

## 5. Phased remediation plan

Effort: S ≤ ½ day, M ≤ 2 days, L > 2 days. Each phase is independently shippable; each item is one PR unless noted. Run the full suite and both doc checkers before every push.

### Phase 0 — Safety (do first, ~2 days total)
| # | Change | Fixes | Effort |
|---|---|---|---|
| 0.1 | `core/hook_context.hook_allow`: omit `permissionDecision` unless a decision is intended; `hook_block` → `permissionDecision: "deny"` (keep exit 0). Add `core/test_hook_context.py`. **Then verify in a bootstrapped repo in default permission mode that an unlisted Bash command prompts again** and that the push gates still block. | C-1 | S |
| 0.2 | GUI permission MCP fails closed: `decide()` denies when `APIARY_PERMISSION_MCP_URL` is unset (explicit `APIARY_PERMISSION_MCP_ALLOW_ALL=1` for tests only); set the `APIARY_PERMISSION_MCP` env flag *after* `bridge.start()` succeeds; move `permission_mcp_config.json`/log under `state_dir()`, cap the log, redact `Write.content`. | C-2 | S |
| 0.3 | Secret scanner: fix `_GENERIC_ASSIGN` (`(?<![A-Za-z0-9])` instead of `\b`, allow key prefixes, widen quoted-value class); scope `_INDIRECTION` to the value token; add AWS-secret, `github_pat_`, `sk_live_`, `npm_`, Slack webhook, JWT rules to `core/secret_patterns.py`; `_git` failure → exit 2 (fail closed); real redaction; length-aware entropy gate. Add each current miss as a regression test. | C-… | M |
| 0.4 | Budgeter hooks: wrap `main()` in try/except (all three); atomic baseline write (`tempfile`+`os.replace`); raise `post_tool_use` stdin cap; dedupe transcript usage by `message.id`; skip logging when `tokens_now == baseline`; count `cache_creation_input_tokens`. | B1 B2 B3 B5 B6 | S |
| 0.5 | GUI: byte-mode transcript attach (`_pos = len(raw)`); backend rejection of `\x03` in `send_control/send_text/send_bytes`; `proc.close(force=True)` on stop and verify the grandchild dies. | gui #2 #3 #4 | M |
| 0.6 | Remove `git push` from `runner/approval.py` and `git add -A` from `auto_harden.py`; pass `--disallowedTools "Bash(git push*)"` and `--max-turns` in `claude_subprocess.run_claude`. | runner Bug 9, security | S |

### Phase 1 — Unbreak what is silently broken (~2 days)
| # | Change | Effort |
|---|---|---|
| 1.1 | `core/flags.py` gets `__main__` (`toggle/enable/disable/status <name>`); one `/budgeter <log|warn|session-warn>` skill via the launcher; delete the three `~/.claude` one-liners; fix SETUP.md:337 and slash-commands.md. | S |
| 1.2 | Move `incubator verify` into the Subcommands table in `cli-tools.md` (unblocks `git push`); add `check_cli_claims.py` to `docs/hooks/pre-commit`; remove `apiary` from `SKIP_HEADERS`; document `doctor stale`. | S |
| 1.3 | Forward `--fix` through `apiary doctor`; add `core/test_cli.py` covering every verb's argv. | S |
| 1.4 | `python -m runner.cron_health repair --apply` after confirming the registered commands; decide whether compass weekly synthesis should run at all (§6). | S |
| 1.5 | Scribe: make `update_note` archive-aware (or have every `cmd_*` check the return and exit 1); archive "done" by `status_changed_at`; stop auto-archiving inside `list` (add `notes.py tidy`); fix `/notes learning` → `learnings`; add `notes.py mark-reviewed` and point `/review-learnings` at it via the launcher. | S |
| 1.6 | `researcher/_yaml_mini`: symmetric quote handling, `#` is a comment only at line start or after whitespace; round-trip tests for `:`, `#`, URLs, quotes. (Or replace with `core/frontmatter.py` in Phase 3 — but fix the corruption now.) | S |
| 1.7 | `monolithic_executor.py`: stamp `schema_version`, assert the plan's; add a test feeding its artifact into `auto_harden.main`. Consider flipping the default back to `executor` (per-step) until monolithic has a completed run. | S |
| 1.8 | Compass `backfill.py`: `captured_at` from transcript mtime. `synthesize.py`: cap observations, atomic write. | S |
| 1.9 | Incubator `_migrate_spec`: `--content-file`; catch `OSError`; recovery hint = "close the original" only; fix `CLAUDE.md.tmpl` (`--since 7d`) and `gitignore.tmpl`. | S |
| 1.10 | `core/session.py` + `save_transcript.py` + `startup.py`: write identity/flags/history under `<repo>/.claude/apiary/session-tmp/` and `.repos/<slug>/sessions/`; wrap `save_transcript.main`; prune the 4,169 stray files in `~/.claude` once (with the human's OK). Makes "apiary writes nothing to `~/.claude`" true. | M |

### Phase 2 — Delete dead weight (~1–2 days; ≈6–8k lines)
All grep-verified; do it in a few themed PRs with the suite green after each.
- **core:** `mailbox.py` (+ `apiary mailbox`, `doctor mailbox`; drift applies the registry update inline under the lock it already holds — `drift.py:138`), `targets.py`, `config.py`, `transcript.py`, `hooks/extract_transcript.py`, `hooks/check_install.py`, `hooks/check_install_stop.py`, `hooks/startup_hook.py` (+ their `hooks_factory` registrations, the triple `learnings_inject` registration → gate on flag), `launcher_template.render`, `_APIARY_OWNED_KEYS` (or use it — Phase 3), `hooks_lib.hook_cmd` global branch.
- **budgeter:** estimator rules, `estimate_magnitude`, `tune.py`, feedback JSONL, `predicted_cost/warning_fired/scope_flags` fields, `budgeter-warn` flag, `_CONT_INSTRUCTION` and the inheritance branches, `save_snapshot/load_snapshot/delete_snapshot`, `count_entries`. Keep `session_length_nudge`.
- **runner:** `run_tracker.py` (or make reachable in Phase 3), `usher_order.py` + `usher_order*.json`, `stage_review_artifacts`, `short_uuid`, `run_history.read_entries`, `overnight.jsonl` double-write, `abort._save_blocker_note`, `extract_usage_block`, `cron_setup.md`, stranded `runner/{executions,plans,specs,hardens,reports}/*` (gitignored), `validate_plan.py:910-916` pass-loop, "Chained executor" strings, DEBUG prints in `auto_harden.py:291-292`.
- **scribe/knowledge:** template gate (unless finished in Phase 3), `cmd_migrate`, `_repo_scribe_dir`, `handoff-sessions`, legacy int-ID map (after confirming no live note references it), `refiner/round_counter.py` (refiner imports harden's or a `core/` copy).
- **gui:** `diag_pty.py`, `_ring`/`buffer`, `poke`, `on_skip` (or wire), `ping/list_sessions/restart_pty/set_session_setting` (or add the restart button the toast promises), `repo_registry.CONFIG_PATH`+`_load_legacy_list`, unused imports, "Phase 2/3" comments.
- **scripts/root:** `bootstrap.py`+test, `uninstall_hooks.py`+test, `install_context_rules.py`+test, `audit_portability.py`, `setup.py`, `.apiary.pre-migration/`, `.apiary/screenshot.png`, root `.claude-session-identity.json`, `.gitignore` duplicates + line 11 + 62-63; every `MIGRATION-PLAN.md` reference (15 files); `git push origin --delete` the 12 merged remote branches.
- **README:** delete the Repository Structure section (or generate it).

### Phase 3 — Consolidate (~2–3 weeks)
| # | Change | Effort |
|---|---|---|
| 3.1 | **Hook dispatcher**: `core/hooks/dispatch.py {pre,post,stop,prompt}` runs the relevant hook modules in-process (each wrapped fail-open, logging to `<repo>/.claude/apiary/hooks.log`); `hooks_factory` registers one entry per event; launcher uses `runpy` instead of a second subprocess. Once-per-session guard for drift check. Target: ≤2 interpreter starts per tool call. | M |
| 3.2 | `core/utils/`: `git_root(start)`, `read_json_object`, `write_json_atomic`, `now_iso`, `MAIN_APIARY_UID`, `resolve_state_dir` — delete the 8/5/7/3/6 copies across core, scribe, compass, researcher, captures, gui. `resolve_apiary_repo` must not prefer the source tree it runs from over the registry (worktrees create a second registry today). | M |
| 3.3 | `core/frontmatter.py` — one dialect for scribe learnings, researcher, captures, memory files, templates, docs. Migrate existing files with a script. | M |
| 3.4 | One sidecar store (`core/sidecar_store.py`) instantiated by researcher and captures; shared CLI helpers; add a `/captures` skill. | M |
| 3.5 | Split `scribe/notes.py` → `scribe/policy.py` (auto-archive), `scribe/maintenance.py` (repair, backup+restore, mark-reviewed, retrotag), `scribe/infer.py` (claude tagging, off by default for `/wrapup`), thin argparse. Hold the FileLock across read-modify-write; write body before index; `os.replace` for archive moves; lazy `ensure_layout`. | L |
| 3.6 | Runner `stage_lib.py` (`run_claude`, `extract_json`, `retry_until_valid`, `check_uuid_safe`) + finish `git_lib.py`; one branch per run (executor works on the worktree's branch); `validate_plan` takes the repo root from the plan; `detached_lib._git` mandatory cwd; worktree dir mismatch fixed; failure recovery recorded before `git_setup_failed`; `encoding="utf-8"` on every subprocess; emit usage on non-zero exit; `run_lock.update()` refreshes `started_at`; collapse the five ticket CLIs into `ticket.py`. Split `_run_detached_impl` and the three `main()`s >200 lines. | L |
| 3.7 | GUI: lock `App` mutations, resolve `active` by id, one `_replay_active()`, `test_app_sessions.py`; extract `appendMessage` reconciliation and the thinking-bubble state machine into Node-tested modules; then split `app.js` into per-concern IIFEs with one `dispatch()`. Pin PyInstaller in a `build` group; stamp git SHA into the build. | L |
| 3.8 | `harden/orchestrate.py` owns path selection, size check, cost estimate, worktree, validate/retry/degrade, budget abort, TODO filing; `harden.md` shrinks to ~100 lines. Same pattern for `/wrapup` Step 4 → `compass/capture.py`. Drop `AskUserQuestion` mandates. | L |
| 3.9 | `core/install.py`: merge profile keys honouring `_APIARY_OWNED_KEYS` (Bug 7); mark generated hook entries explicitly and match only that in `is_apiary_entry` (Bug 8); reconcile self-pointer uid with registry + `doctor pins` check (Bug 4/5); uninstall order files-first, registry-last, refuse on main-apiary (Bug 6); wrap CLAUDE.md/bootstrap-state failures in `InstallError` (Bug 10). | M |

### Phase 4 — Engineering infrastructure (~2 days)
- CI: one GitHub Actions workflow, `ubuntu/windows/macos` × Python 3.11/3.12: `poetry install`, `poetry run pytest -q`, `python docs/check.py`, `python docs/check_cli_claims.py`, `python scripts/secret_scan.py --path .`, `node gui/web/test_*.js`. This is the only way `install.sh` and the POSIX hook path ever get exercised.
- `ruff` (`E,F,I,PLW1514,S602,S605`) + `ruff format`; `pyright` basic later. Fix the findings in the same PR or baseline them.
- `pyproject.toml` → `[project]` table; fix `packages`; `pytest-cov` (report only); `* text=auto` in `.gitattributes`.
- Test infra: shared git-repo fixture for `core/test_{install,drift,cascade,uninstall}.py` (suite 2.5 min → <1 min); `runner/test_orchestrator.py` patches `LOCKS_DIR`; scrub inherited `APIARY_*`/`CLAUDE_PROJECT_DIR` in hook test helpers; a pytest test that shells to `node` for the JS suites; **one hermetic end-to-end runner test with a fake `claude` on `PATH`** driving all six real stages; execute the generated launcher in a test.
- Versioning: implement `apiary update` (~60 lines chaining `migrations/`) *or* delete `migrations/`, `doctor versions`, `version.json`. Tag `v0.1.0`. Fix `code-style.md` ("unittest-style classes, executed by pytest").

### Phase 5 — Docs (~2 days, after Phases 1–3 so you document reality)
- Generate `docs/reference/cli-index.md` and the `cli-tools.md` tables from `check_cli_claims.introspect()`; fix the bare `python docs/reference/cli_lookup.py` instruction in `startup_prompt_hook.py:218`.
- Sweep the 60 stale statements (appendix `infra-docs-skills.md` §4 is the checklist): `~/.claude` → per-repo; `setup.py` → `core/install.py`/`hooks_factory.py`; `/startup` → startup hook; remove `APIARY_STATE_LAYOUT`, `apiary_bootstrap`, `apiary update` (unless built), `MIGRATION-PLAN.md`; runner artifact paths; `hooks.md` Stop semantics and the real 15-hook order; budgeter config tables (`min_tasks`, `expensive_percentile` etc. do not exist — real keys are in `budgeter/config.json`); rewrite `hook-lifecycle.md`; `system-overview.md` for 11 tools; `per-repo-install.md` for the post-mailbox model; `gui/README.md`'s nine mismatches; scribe archive policy (context 3 d, decision 30 d, done 1 d after *completion*, handoffs all-but-latest, todo/wishlist/blocker never).
- Shrink `docs/check.py` to: frontmatter present, index complete, `last_verified` not older than the file's last git change; derive the tool list from the tree. Fix `remind_standards.py` `known_dirs`.

### Phase 6 — Strategic (requires the human's decision; see §6)
Runner scope · GUI cost ceiling · compass keep/kill · budgeter warning removal (recommended: delete) · harden orchestration in Python (recommended: yes).

---

## 5a. Amendments agreed in the 2026-08-26 walkthrough

The owner went through every section of this review and settled the open items. These amendments override anything above that conflicts with them.

**Status at time of writing:** Phase 0.3 (secret scanning) is merged as PR #31 (`1728c5f`); the `check_cli_claims` drift that blocked pushes is cleared; the stray `~/.claude` files were deleted (4,027 removed, files < 24 h old kept). Everything else is not started.

### A. Permissions (Phase 0.1) — approved, with a fallback
Fix `hook_allow` so hooks stop voting `allow`; verify empirically that prompts return in default mode. The owner runs mostly in auto mode and values autonomy; if the fix produces annoying prompts, the sanctioned answer is `permissions.allow` rules in the apiary profile (built from transcripts via `/fewer-permission-prompts`) — **never** a hook vote. Do not re-introduce a blanket `allow`.

### B. Scribe templates (add to Phase 1) — option C
Keep the template gate's *required-sections* check; **delete the hash-ack** (`--ack-template`, `template_hash`, the two-attempt flow). Ship one template per note **type** under `scribe/default_templates/` and scaffold them into `<state-dir>/scribe/templates/` at bootstrap/self-bootstrap. `required:` is non-empty only where structure earns it: handoff (`What was done / Key decisions / What's pending / Where it stopped` — must match `core/commands/wrapup.md`), decision (`Context / Decision / Why / Consequences`), blocker (`Blocked on / Tried / Unblock when`). todo / wishlist / context / general get guidance-only templates with no `required:`. `--force` stays but is logged. Forward-only: existing notes are never validated or rewritten. Evidence for doing this: of the last 60 handoffs, ~32 follow the wrapup structure and ~26 are free-form.

### C. Duplication prevention (add to Phases 3 and 4) — approved
Three layers, because the doc-only rule (`code-style.md` "Reuse core/") demonstrably failed:
1. One `core/utils/` with guessable names and one `core/frontmatter.py`; no other definitions of those names anywhere (Phase 3.2/3.3).
2. A **duplicate-helper hook** inside the dispatcher (Phase 3.1): before `Write`/`Edit`, if the content defines a function whose name (or close variant) already exists elsewhere in the repo, inject a one-line "`X` already exists at `path:line` — reuse or say why not." Non-blocking. Indexes definitions once per session. Works in every bootstrapped repo.
3. An **AST near-duplicate check** (stdlib): normalise function bodies, hash, report identical/high-overlap pairs across files. Runs in CI (Phase 4) and may run in pre-commit for this repo.
Plus parity tests wherever two components share a contract (model: `core/test_secret_patterns.py`).

### D. Docs, long-term (amend Phase 5) — approved
Rule: **a doc that can drift is generated from code or tested against it; everything else stays short.** Concretely:
1. Generate `cli-index.md`, the CLI flag tables, the hooks table, the slash-command list, config keys, storage paths, and the archive policy from code; render the session-start index at session start rather than storing it.
2. Extract every ```bash block that invokes an apiary CLI and run it in CI with `--help`/`--dry-run`.
3. A change-mapping enforced in pre-commit: a change to a mapped code file requires touching its architecture doc in the same commit (or an explicit `docs: unchanged` trailer); plus `last_verified` older than the file's last git change fails.
4. Extend `context_rule_error_reminder` so a doc-shaped failure ("unrecognized arguments" on a documented command, missing documented path) files a scribe todo naming the doc and line.
Delete prose that restates code; keep architecture docs short and dated; the review appendices are dated snapshots and should say so.

### E. Hook dispatcher (Phase 3.1) — approved
One registration per event, hooks run in-process, launcher runs targets in-process; delete `check_install`, `check_install_stop`, `startup_hook`; register `learnings_inject` once. Land after A, so the dispatcher never reproduces the blanket `allow`. Re-bootstrap all registered repos afterwards.

### F. Engineering plumbing (Phase 4) — approved as written
CI (3 OS × 2 Python), ruff + format, pytest-cov report-only, `[project]` metadata + packages fix, `* text=auto`, shared git fixture, JS tests wired in, one hermetic end-to-end runner test.

### G. Runner — **REVIVE to full potential** (supersedes §6 option (a))
The owner's reason for not using it is exactly the flakiness this review found; the goal is "works as intended." Order of work:
1. First PR: the hermetic end-to-end test (fake `claude` on `PATH`, all six real stage scripts, temp target repo). It must fail on today's code (C-3) and pass after 2.
2. Correctness (from `subsystems/runner.md` §9): `schema_version` in `monolithic_executor` (or make per-step the default until monolithic has a completed run); one branch per run; `validate_plan` target-aware; `detached_lib._git` mandatory cwd; worktree-dir mismatch fixed; failure recorded before `git_setup_failed`; resume honours `valid`/`status`; `run_lock.update()` refreshes `started_at`; `encoding="utf-8"` everywhere; usage emitted on non-zero exit and timeouts counted; `approval` never pushes; `auto_harden` never `git add -A`; `claude_subprocess` passes `--disallowedTools` (git push, writes outside cwd) and `--max-turns`; scribe notes via the launcher; `queue.py` joins by UUID; `stage_lib.py` + finished `git_lib.py`; split the >280-line functions; delete `usher_order`, the `overnight.jsonl` double-write, stranded artifacts.
3. **Acceptance ("works as intended"):** ten consecutive nightly runs against a real backlog of ≥5 small tickets in which every run either completes with a review-ready branch or fails with a recorded, resumable reason; the morning `queue.py` table is correct for every row; no orphaned worktrees or branches; cost stays within cap including failed calls; nothing is pushed. Only then add features.

### H. Compass — **keep and fix, and measure** (supersedes §6 option list)
Fix 1.4 (cron) and 1.8 (`captured_at`, cap, atomic write); add tests for `synthesize`/`backfill`; put injection behind a flag. Then answer "is it working?" with three instruments:
1. **Offline predictive validity** (`compass/evaluate.py`): hold out recent transcripts; truncate each before a user reply; ask a model to predict the reply's traits (approve / push back / redirect; terse / verbose; asks why) with and without `personality.md`; score against the actual reply. No lift → the profile carries no signal. Cheap, repeatable, runs in CI on fixtures.
2. **Live A/B**: alternate the injection flag per session (or per day); stamp `compass_injected` into the budgeter log; per session count clarifying questions asked, user corrections/pushbacks (keyword heuristic over user turns), `AskUserQuestion` uses, turns to first tool call. Compare after ~30 sessions each arm.
3. **Health in `doctor` and the startup banner**: profile age, active observation count, last synthesis; warn when > 14 days stale; self-consistency check (synthesise from two disjoint halves of observations, compare per dimension).
Set a review date when the instruments land; decide keep/remove on the numbers.

### I. Versioning — **build it** (supersedes §6 item 7)
Implement `apiary update` (~60 lines chaining `migrations/`), make `version.json` actually read on session open, tag `v0.1.0`, bump on the next layout change.

### J. Settled by earlier sections
Budgeter warnings + `[CONT]`: delete. GUI: keep with a ceiling (three HIGH bugs, split `app.js`, no new TUI-scraping). Harden orchestration → Python. Mailbox: delete. `~/.claude` writes: move to per-repo (1.10); the existing stray files are already gone.

## 6. Decisions — record (settled 2026-08-26)

| # | Decision | Outcome |
|---|---|---|
| 1 | Runner scope | **Revive** to full potential, e2e test first, ten-night acceptance (§5a-G) |
| 2 | GUI | Keep with a cost ceiling |
| 3 | Compass | Keep, fix, **measure** (§5a-H) |
| 4 | Budgeter warnings | Delete; keep logger + session nudge |
| 5 | Harden orchestration in Python | Yes |
| 6 | `~/.claude` stray files | Deleted 2026-08-26 (4,027 files) |
| 7 | Versioning | Build `apiary update`, tag |
| 8 | Permissions fallback | Allow rules, never hook votes (§5a-A) |
| 9 | Scribe templates | Option C, one per note type, forward-only (§5a-B) |

---

## 7. Conventions for executing this plan

- **Branch per change**, never commit on `master` (`review/deep-review-2026-08` holds this document). PR to master; the human merges or authorises `gh pr merge`.
- Before every push: `poetry run pytest -q` (expect ≥1,696 passing), `poetry run python docs/check.py`, `poetry run python docs/check_cli_claims.py` (must be 0 — see 1.2), `poetry run python scripts/secret_scan.py --staged`.
- Never write under `.claude/` from an agent (the protect-self gate prompts every time); use repo-root `_tmp_*` for scratch.
- Use `poetry run python …` for everything; never bare `pip`.
- Portability rules in `PORTABILITY.md` apply: list-form subprocess, `encoding="utf-8"`, `pathlib`, no absolute paths, no `shell=True`.
- Hooks must not crash and must not vote on permissions.
- When you delete a copy of a helper, delete *all* copies (X-3). When you touch a doc claim, verify it against code first (X-5).
- File a scribe todo for any out-of-scope bug you find; do not fix it inline.
- The six appendices are the authoritative detail; if this document and an appendix disagree on a line number, trust the appendix and re-verify.
