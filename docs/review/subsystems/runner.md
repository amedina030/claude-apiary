---
type: architecture
title: "Runner subsystem review"
scope: project
description: Deep review of runner/: six-stage autonomous orchestrator, detached mode, schedulers (2026-08-26 deep review, subsystem appendix)
framework_version: "1.0"
last_verified: 2026-08-26
---

# Code review: `runner/` (claude-apiary)

Read-only review of the autonomous six-stage orchestrator. Every non-test `.py` file under `runner/` was read in full; tests were skimmed for shape and run once (`poetry run pytest runner -q` → **594 passed in 35.5s**). Nothing was edited, committed, or executed beyond the test suite and read-only git queries. All paths are repo-relative to `D:\Professional\claude-apiary`.

---

## 1. What it is

`runner/run.py` is a subprocess-driven orchestrator. `STAGES` (`runner/run.py:83-90`) is a six-tuple list; for each stage it spawns `python -m runner.<module> <input-artifact>` (`runner/run.py:975-979`), captures stdout/stderr, scrapes every `<usage>…</usage>` block out of the combined text (`runner/run.py:292-359`), pipes each block to `budgeter/log_agent_cost.py` (`runner/run.py:218-240`), and checks a cumulative token cap. Stage 4 is either `monolithic_executor` (default — one `claude -p` call for the whole plan) or `executor` (one call per step), chosen at import time from `config.json` `executor.mode` (`runner/run.py:78-81`).

Stages communicate **only through JSON files keyed by UUID**: `intake/<uuid>.json → specs/ → plans/ → executions/ → hardens/ → reports/`. Each producer stamps `schema_version` and each consumer calls `assert_schema_version` (`runner/schema_versions.py`). Between LLM stages sit deterministic validators (`validate_intake.py`, `validate_spec.py`, `validate_plan.py`) that the LLM stage re-invokes as a subprocess inside a 3-attempt retry loop, feeding the validator's stderr back into the next prompt (`runner/auto_refine.py:255-312`, `runner/auto_plan.py:586-650`).

State lives under `artifacts_root()` (`runner/target_repo.py:128-144`): `$APIARY_TARGET_STATE_DIR/runner/` when invoked via the per-repo launcher, else `<target>/.apiary/runner/`. That root holds `backlog/`, `intake/`, the five artifact dirs, `locks/` (crash lockfiles), `runs/` (cross-invocation token/attempt tracker), `logs/`, `run_history.jsonl` and the legacy `overnight.jsonl`.

Each `claude -p` call goes through `runner/claude_subprocess.py:108-172`: prompt on stdin, `--output-format json`, an allowlisted environment, a 50 MB output cap, and `<usage>` emission to stderr when the exit code is 0.

Detached/cron mode (`runner/run.py:450-941`) adds: a Windows Job Object so killing `run.py` kills every child (`runner/run.py:108-175`), SIGINT/SIGTERM/SIGBREAK handlers, a hygiene precheck (skip if ≥ N unmerged `runner/*` branches), ticket selection from `usher_order.json` then `backlog/`, a sizing gate (`runner/usher.py`), a per-UUID lockfile, a cross-invocation tracker with `max_restarts`/token cap, a fresh git worktree at `<target>/.runner-worktrees/<branch>` on branch `runner/<slug>-<uuid>`, artifact-based resume, a bundle commit, worktree teardown on success, and a `run_history.jsonl` entry. `cron_health.py` + `schedulers/windows.py` reconcile a per-host registry (`cron_registry/<hostname>.json`) against Windows Task Scheduler.

---

## 2. Architecture assessment

### Stage decomposition: sound in principle, leaky in practice

The "each stage is a CLI that reads one JSON and writes one JSON" contract is a good idea: every stage is independently runnable, and `--resume-from` works because the artifacts are on disk. But the contract leaks in several ways:

- **Stages share state through git, not just JSON.** The executor creates branch `runner/<uuid>` by `git checkout -b` in whatever cwd it is given (`runner/executor.py:856-873`, `runner/monolithic_executor.py:348-363`); `auto_harden` and `approval` then operate on "the current branch". In detached mode this means the worktree (created on `runner/<slug>-<uuid>`, `runner/run.py:639,707`) ends up on a *second* branch `runner/<uuid>` that the orchestrator never named (see Bug 3).
- **Stages reach around the artifact they were given.** `auto_harden` re-opens the plan to get the spec (`runner/auto_harden.py:390-398`); `approval` re-opens intake, spec, plan and execution (`runner/approval.py:270-278`); `run_tracker.get_resume_stage` probes all five dirs (`runner/run_tracker.py:79-98`). The "input artifact" is really "the UUID".
- **The orchestrator and the stages disagree about what a valid execution artifact is.** The default executor never writes `schema_version` (`runner/monolithic_executor.py:375-381`), while the consumer requires it (`runner/auto_harden.py:350-354`). See Bug 1.
- **Path resolution is split three ways.** `validate_plan` resolves file paths against apiary's repo root (`runner/validate_plan.py:29,143-152`); the executor checks post-conditions against `Path.cwd()` (`runner/executor.py:1052`, `runner/monolithic_executor.py:259,276`); the orchestrator sets cwd to the worktree (`runner/run.py:809`) or the target repo (`runner/run.py:1440`). These only coincide for the apiary-self case on `master`.

### Duplication across stages

Concrete counts (each a separate copy, not a shared helper):

| Concern | Copies | Locations |
|---|---|---|
| `run_claude` wrapper | 5 | `auto_refine.py:154`, `auto_plan.py:340`, `executor.py:685`, `auto_harden.py:79`, `approval.py:121` |
| Envelope/fence/prose JSON extraction | 5, all different | `auto_refine.extract_spec:160`, `auto_plan.extract_plan:381`, `executor.parse_verify_output:619`, `auto_harden.extract_text/extract_json_from_text:84/95`, `approval.extract_text/extract_json_from_text:127/138` |
| UUID path-traversal guard | 6 | `run.py:591-597`, `run.py:1344-1350`, `executor.py:842-849`, `monolithic_executor.py:337-344`, `promote.py:35-42`, `mark_done.py:23-30` |
| `git` wrapper | 3 | `git_lib.git:14`, `detached_lib._git:49`, `close_source_todo._git:42` |
| `branch_exists` / `checkout` / `get_current_branch` | 2–3 each | `executor.py:62,285`, `auto_harden.py:51,56,72`, `approval.py:52,56` — and `auto_harden.branch_exists` lacks the `refs/heads/` fix that `executor.branch_exists` got (ATK-006) |
| `slugify` | 3 different implementations | `detached_lib.py:40`, `draft_ticket.py:26`, `refine_to_intake.py:127` |
| `read_todo` / `read_note` | 3 | `draft_ticket.py:36`, `create_intake.py:37`, `refine_to_intake.py:35` |
| `validate_intake` invoked as a subprocess instead of importing `validate()` | 3 | `promote.py:75`, `create_intake.py:137`, `refine_to_intake.py:207` |
| LLM retry loop (attempt / spawn / parse / validate / best-attempt) | 2 near-identical ~70-line bodies | `auto_refine.py:255-323`, `auto_plan.py:586-661` |
| "skipped" `history_append` dict literal | 10 | `run.py:491,541,558,577,599,617,653,667,688,710` |
| Squash-merge-and-push block | 2 × ~55 lines | `approval.py:303-363` and `approval.py:388-444` |

`git_lib.py` (44 lines) was created to end exactly this drift ("Consolidating both here removes the drift risk", `runner/git_lib.py:4-9`), but only `git()` and `format_git_error()` moved.

### Is `detached_lib` / `queue` / `cron_health` / `schedulers` / `run_lock` / `run_tracker` / `abort` / `usher_order` over-engineered?

For one user on one Windows box: yes, and the evidence is that most of it has never run against reality.

- **Exercise evidence.** All runner-produced commits in `git log --all` fall between **2026-04-06 and 2026-04-13** (33 `runner/<uuid> step N` commits, 6 `harden round` commits, 4 bundle commits). The in-repo `runner/executions/` etc. contain 12 artifacts dated Apr 6–10 (stranded there by the state-dir migration, commit `e887b17`). The live state dirs are empty: `.repos/claude-apiary-1/runner/` has only empty `intake/ locks/ logs/`, `.apiary/runner/` has only `locks/`, and **no `run_history.jsonl` or `overnight.jsonl` exists anywhere**. There are zero `runner/*` branches and no runner worktrees. Nothing has completed since mid-April.
- Meanwhile, everything from `b0afdcc` (schema versions, Apr 14) onward — monolithic executor as default, crash lockfiles, abort, cross-invocation tracker, cron_health/schedulers, multi-repo target plumbing, the `.apiary/runner` state move — landed **after** the last successful run and has never been validated end-to-end. `test_run_detached.py` mocks `git_worktree_create`, `prune_stale_worktrees` and `run_stage` in every test (`runner/test_run_detached.py:58,163-842`), so the pieces have only ever met each other in mocks.
- `usher_order.py` has no CLI; `register_group`/`register_standalone`/`archive_order` are called only from tests. The only manifest is the hand-written, git-tracked `runner/usher_order.json` (one group, one completed and one failed ticket from April).
- `run_tracker` (restart/resume across nights) and `prune_stale_worktrees` are unreachable in practice (Bugs 4 and 5).
- `cron_health` + `schedulers/` (~840 lines incl. tests) manage two Task Scheduler entries. The registry approach is reasonable, but the `SchedulerBackend` Protocol with one implementation, a frozen dataclass registry model, and a CSV parser for `schtasks /query /v` is a lot of scaffolding for `schtasks /create` once.

### Windows-only scheduler: honest V1 or portability lie?

Honest, and clearly labelled: `get_scheduler` raises `UnsupportedPlatformError` on non-Windows (`runner/cron_health.py:187-192`), the docstring and `docs/reference/cli-tools.md:865-869` say "Windows Task Scheduler only in this release", and only `daily` schedules are supported (`runner/schedulers/windows.py:232-238`). The rest of the runner *is* cross-platform in intent (POSIX env allowlist, `os.kill` liveness, signal handling). The portability problem is elsewhere: `text=True` without `encoding=` in every git/stage subprocess (see Bug 7), which the repo's own standard forbids (`docs/standards/code-style.md` "UTF-8 everywhere").

### Is the design fundamentally sound?

The core loop (LLM produces JSON → deterministic validator rejects → LLM retries with the errors) is the strongest idea here and it demonstrably worked in April. The executor guardrails (`assert_files_clean`, `assert_no_unexpected_writes`, `_assert_action_matches_staged`, post-conditions, `validate_resume_state`) are thoughtful and well tested. But the system as a whole is now a large amount of machinery whose value is unproven: the default executor cannot reach stage 5 (Bug 1), the detached retry story is dead (Bug 4), the morning review table cannot join its own data (Bug 3), auto-merge never fires overnight (`approval` always takes `worktree-deferred` in a worktree, `runner/approval.py:308-311`), and multi-repo targets fail plan validation (Bug 2). Five months of feature work sits on top of a pipeline that has not been run.

---

## 3. Bugs and correctness risks (ordered by severity)

### Bug 1 — Default executor produces an artifact the next stage rejects (pipeline cannot complete)
- `runner/config.json:17` sets `"mode": "monolithic"`; `runner/run.py:78-81` therefore drives stage 4 with `monolithic_executor`.
- `runner/monolithic_executor.py:375-381` builds `execution_log = {"uuid","branch","mode","status","steps"}` and never sets `schema_version` (compare `runner/executor.py:913-919`, which does).
- `runner/auto_harden.py:350-354` calls `assert_schema_version(execution, "execution", 1)` → `got=None` → `SchemaVersionError` → `sys.exit(1)`.
- Outcome: every run in the default configuration ends `stage_failed:auto_harden`. `run_tracker.get_resume_stage` then sees `executions/<uuid>.json` and resumes at `auto_harden` (`runner/run_tracker.py:79-98`), which fails identically. `approval.load_artifact(exec_path, …, EXECUTION_SCHEMA_VERSION)` (`runner/approval.py:278`) would also reject it.
- `monolithic_executor.main` also skips `assert_schema_version(plan, …)` that `executor.main` performs (`runner/executor.py:825-829` vs `runner/monolithic_executor.py:322-330`).
- No test covers this seam: `test_monolithic_executor.py` never mentions `schema_version`; `test_orchestrator.py` mocks `run_stage`. Timeline: `59652ea` (monolithic) predates `b0afdcc` (schema versions), and the latter missed it.

### Bug 2 — `validate_plan` validates against apiary's checkout, not the target repo or the worktree
- `_REPO_ROOT = Path(__file__).resolve().parent.parent` (`runner/validate_plan.py:29`) is used by `_resolve_repo_path` (`:143-152`), the modify/delete existence check (`:903-908`), the gitignore check (`:431,467`) and the `git grep` in `_check_removal_coverage` (`:731-735`). `auto_plan.validate_plan()` runs the validator with `cwd=REPO_ROOT` (apiary) too (`runner/auto_plan.py:537-540`).
- Failing input: any `--target-repo X` (or intake `target_repo`) whose plan has a `modify` step on `src/foo.py` that exists in X but not in apiary → `step[i]: file not found: src/foo.py` on all three attempts → `stage_failed:auto_plan`. Only the banned-token list is target-aware (`:83-107`), contradicting commit `8094e35` "target-aware auto_plan + validate_plan" and `docs/reference/cli-tools.md:616-618`.
- Same root cause in `detached_lib._git` (default `cwd=REPO_ROOT`, `runner/detached_lib.py:49-52`): `hygiene_precheck`, `pick_backlog_item`, `_branch_exists_for_uuid` and `prune_stale_worktrees` all inspect **apiary's** branches even when the run targets another repo.

### Bug 3 — Detached runs produce two branches and the morning `queue.py` table cannot join its own data
- `run.py` creates the worktree on `runner/<slug>-<uuid>` (`runner/run.py:639,707`; `runner/detached_lib.py:178`).
- Inside it, stage 4 runs `git checkout -b runner/<uuid>` (`runner/executor.py:856,868-870`; `runner/monolithic_executor.py:348,358-363`) and commits there. On success the executor exits without switching back, `auto_harden` restores "original" = `runner/<uuid>` (`runner/auto_harden.py:401,495`), and `git_commit_all_in` bundles onto `runner/<uuid>` (`runner/run.py:864`). `runner/<slug>-<uuid>` is left pointing at `master`.
- `run_history`/`overnight.jsonl` records `'branch': runner/<slug>-<uuid>` (`runner/run.py:910`). `queue.py` keys entries by that branch (`runner/queue.py:41-43`) and iterates `list_unmerged_runner_branches()` which returns `runner/<uuid>` (`runner/queue.py:67,75`) → every row shows `TICKET unknown`, `STAGES unknown`, `TOKENS unknown`, `STATUS unknown`.
- On executor failure the executor checks out `runner/<slug>-<uuid>` (`runner/executor.py:1182`), `run.py` commits `--allow-empty` there (`runner/detached_lib.py:221`), so **both** branches now have commits beyond master and each failed run consumes two of the `max_unreviewed` slots (`runner/detached_lib.py:61-78`).
- `run_cleanup` and `abort` know about both conventions (`runner/run.py:1002-1025`), which confirms the double-branch was noticed and papered over rather than fixed.

### Bug 4 — Cross-invocation retry/resume is unreachable; failures repeat nightly without being counted
- Any failure preserves the worktree (`runner/run.py:878-885`). Next night, `git_worktree_create` returns `worktree path already exists` (`runner/detached_lib.py:176-177`) → `git_setup_failed` and `return 1` at `runner/run.py:707-722` — **before** `run_tracker.record_attempt` (`:896`) and before `order_update_status(..., "failed")`. The tracker's `attempt_count` never grows past 1, `max_restarts` never trips, and an `usher_order` ticket stays `pending` and is re-selected every night (`runner/run.py:515-527`).
- The intended safety valve, `prune_stale_worktrees`, cannot help: it lists worktrees under `WORKTREES_DIR = <target>/.apiary/runner-worktrees` (`runner/detached_lib.py:20,244`; `runner/target_repo.py:195-202`), but `run.py` always passes a non-None `target_repo`, so `worktrees_dir_for` puts real worktrees at `<target>/.runner-worktrees` (`runner/detached_lib.py:134-143`). The two never overlap. `test_detached_lib.py:180-181` and `test_target_repo.py:267` each assert their half of the mismatch.
- Even if the dirs matched, prune preserves any branch with commits beyond master (`:290-293`), which after Bug 3 is every failed run.
- Net effect: `run_tracker.py`, the `_prior_runs` seed (`runner/run.py:740-747`), `get_resume_stage`, and `max_restarts` are dead weight in detached mode.

### Bug 5 — Resume picks the stage that is guaranteed to fail again
- `auto_refine` and `auto_plan` write their best attempt with `"valid": false` on exhaustion (`runner/auto_refine.py:314-317`, `runner/auto_plan.py:653-655`); the per-step executor writes `status: aborted` (`runner/executor.py:1172-1177`).
- `get_resume_stage` only tests `exists()` (`runner/run_tracker.py:95-97`), so a failed refine resumes at `auto_plan` → "Spec is not valid" (`runner/auto_plan.py:569-571`); a failed plan resumes at `executor` → "Plan is not valid"; an aborted execution resumes at `auto_harden` → "Cannot harden aborted execution" (`runner/auto_harden.py:356-358`). Moot today because of Bug 4, but it means fixing Bug 4 alone yields a loop of guaranteed failures until `max_restarts`.

### Bug 6 — Stale-lock heuristic treats any run older than one hour as crashed
- `run_lock.is_stale` returns True when `time.time() - started_at > stage_timeout` (3600 s) even if the PID is alive (`runner/run_lock.py:149-163`); `update()` never refreshes `started_at` (`:45-60`).
- A legitimate run easily exceeds 1 h (refine 3×900 s + plan 3×900 s + monolithic 1800 s + harden …). During that window `run.py main()` refuses to start (`runner/run.py:1280-1293`, arguably fine) but `--abort <uuid>` **succeeds** on the live run (`runner/abort.py:129-133` only refuses when not stale), removing its worktree and deleting its branches while stage subprocesses are still writing to them.

### Bug 7 — Text-mode subprocesses without `encoding=` (locale-dependent decode on Windows)
- `runner/run.py:975-979` (`Popen(..., text=True)`), `runner/git_lib.py:21-24`, `runner/detached_lib.py:52`, `runner/close_source_todo.py:43-45`, `runner/abort.py:50-54,60-63,73-76`, and the validator subprocess calls in `auto_refine.py:221-224`, `auto_plan.py:537-540`, `auto_harden.py:131-134,146-149,162-165`, `approval.py:111-114`.
- `git log --format=%s` emits raw UTF-8 commit subjects that the LLM authored (plan descriptions become subjects at `runner/executor.py:1070`). Under cp1252 a subject containing e.g. `Á` (`C3 81`) raises `UnicodeDecodeError` in `get_completed_step_numbers` (`runner/executor.py:515`) or `_commits_since_base` (`runner/monolithic_executor.py:170`), crashing the stage. `auto_harden` also writes LLM findings to `NamedTemporaryFile(mode="w")` with no encoding (`runner/auto_harden.py:128,143,159`) → `UnicodeEncodeError` on a `→` in a finding. The repo relies on a user-level `PYTHONUTF8=1` (scribe learning L-2026-1), which an old todo notes is not visible to hook shells; Task Scheduler inherits user env vars, but this is exactly the kind of implicit invariant the code-style standard bans. `schedulers/windows.py:105-109` does it right (`encoding="utf-8"`).

### Bug 8 — Cost accounting is blind to failed or timed-out calls
- `<usage>` is emitted only when `returncode == 0` (`runner/claude_subprocess.py:165-170`). A `claude -p` that times out at 1800 s (monolithic) or 900 s (refine/plan) after consuming tokens returns `(-1, "", msg)` with no usage; the budgeter never sees it and the cap is not decremented. Retried attempts in `auto_refine`/`auto_plan` that exit non-zero are likewise invisible.
- The no-usage fail-closed check only fires when the stage `ok` (`runner/run.py:835-843`), so a stage that failed after heavy spend is recorded as `no_usage` with 0 tokens.
- The cap counts raw `total_tokens` (`runner/cost_emit.py:230-232` sums every numeric usage field, i.e. cache reads at full weight) while the summary computes a weighted figure (`runner/run.py:404-411`); the cap is checked only after a stage returns, so a single stage can spend up to `stage_timeout` unbounded. The effective spend ceiling is wall-clock (~6 h × 6 stages), not tokens.

### Bug 9 — Interactive mode mutates the operator's checkout
- `run.main()` runs stages with `cwd=target_repo_path` (`runner/run.py:1440`), i.e. the main working copy. The executor `git checkout -b runner/<uuid>` there; on success nobody restores the original branch (`original_branch` is only used on abort, `runner/executor.py:859,1182`). `auto_harden.commit_all` runs `git add -A` (`runner/auto_harden.py:62-64`), sweeping any untracked operator files into a "harden round fixes" commit. `approval` then checks out `master`, squash-merges and **pushes** (`runner/approval.py:317-363`), and `original_branch` is assigned twice and never used (`:315,396`). `assert_files_clean` protects only the step's declared files (`runner/executor.py:74-102`).

### Bug 10 — Review artifacts never ride along with the branch
- `stage_review_artifacts` looks for `wt_path/runner/{specs,…}/<uuid>.json` (`runner/detached_lib.py:198-199`), but since `e887b17` all artifacts are written under `artifacts_root()` outside the worktree (`runner/target_repo.py:128-144`; confirmed by `test_artifacts_land_in_apiary_not_worktree`). The function silently stages nothing; the comment at `runner/run.py:859-863` and commit `2ed0d58` describe behaviour that no longer exists. `test_detached_lib.py` passes only because it plants the files by hand.

### Bug 11 — `abort` archives from pre-migration paths and its blocker note is a silent no-op
- `_archive_artifacts` copies `SCRIPT_DIR/<dir>/<uuid>.json` = `runner/specs/…` (`runner/abort.py:26-33`) — the stranded April location, not `artifacts_root()`; `CRASHES_DIR = runner/crashes` (`:21`) likewise lives in the source tree.
- `_save_blocker_note` requires `~/.claude/apiary_launch.py` (`runner/abort.py:112-114`); that global launcher was removed in `f61beb3` and does not exist on this machine, so no blocker note is ever written. `cron_health.py:21-22,413-416` still print that path as the recommended command.

### Bug 12 — Scribe notes from `approval` land in the wrong store
- `write_scribe_note` runs `scribe/notes.py` directly (`runner/approval.py:109-116`) without the launcher, so `APIARY_TARGET_STATE_DIR` is unset and scribe falls back to `<git-root>/.apiary/scribe/` (`scribe/notes.py:158-178`). In detached mode git-root is the worktree, which is deleted on success; in interactive mode it is the legacy in-repo store, not `.repos/claude-apiary-1/scribe`. The "RUNNER COMPLETE"/"PENDING REVIEW" todos are therefore lost or invisible.

### Bug 13 — Stage timeout orphans the grandchild `claude` process
- On `stage_timeout`, `run_stage` kills only the stage's Python (`runner/run.py:987-994`). The `claude` grandchild keeps running (and billing) until `run.py` itself exits and the Job Object closes (`runner/run.py:108-175`, detached only). In interactive mode there is no Job Object at all (`_install_kill_on_job_close` is called only from `run_detached`, `:454-455`). With the per-step executor, 6 steps × 2 retries × 2 no-change retries × 900 s can exceed the 3600 s stage limit.

### Smaller correctness issues
- `auto_harden.MAX_ROUNDS` code default is 3 (`runner/auto_harden.py:37`), config says 1 (`runner/config.json:23`); `run.py` token-cap fallback is 2,000,000 (`runner/run.py:474`) while config says 10,000,000 (`runner/config.json:32`) and docs say 2,000,000.
- `executor.run_test_command`'s `cd <dir> && cmd` handling (`runner/executor.py:698-702`) is unreachable: the validator rejects any `&` (`runner/validate_plan.py:125,321-345`). It also passes a planner-supplied `cwd` unchecked.
- `validate_plan.py:910-916` is a loop whose only body is `pass`.
- `claude_subprocess` truncates output *after* `capture_output` has buffered all of it (`runner/claude_subprocess.py:149-163`); the "prevents unbounded memory growth" comment (`:103-105`) is false.
- `approval.review_deferrals` makes a Claude call with no model pinned and `timeout=120` (`runner/approval.py:121-124`) although the module docstring says "No LLM calls — purely mechanical" (`:9`) and `NO_USAGE_STAGES` lists approval as making none (`runner/run.py:54-59`). Not a cap bypass (usage is emitted), but the model choice is uncontrolled.
- `usher_order.next_eligible` returns the first `pending` standalone ticket without checking for an in-flight branch (`runner/usher_order.py:116-118`), unlike `pick_backlog_item` (`runner/detached_lib.py:100-101`).
- `monolithic_executor.reconstruct_step_results` marks every `test`/`verify` step `passed` unconditionally (`runner/monolithic_executor.py:234-239`); a failing test suite is invisible unless a post-condition catches it.
- Interactive `main()` writes the lockfile before validating `--resume-from` prerequisites? No — but it calls `run_lock.scan_stale()` and `run_lock.write()` against the real `LOCKS_DIR`, and `test_orchestrator.py` does not patch `LOCKS_DIR` (only `test_abort.py`, `test_run_detached.py`, `test_run_lock.py` do), so the orchestrator tests write into and depend on the real `.apiary/runner/locks/` (its mtime changed to 10:22 today when I ran the suite; a real stale lock on disk would fail every `main()` test).

---

## 4. Security / safety

**What is real:**
- Plan-level gates: out-of-repo path rejection (`runner/validate_plan.py:382-411`), gitignored-path rejection (`:414-493`), shell-metacharacter ban for test commands (`:321-345`), banned tokens (`:496-527`), file-overlap ordering (`:594-644`), symbol-removal coverage (`:681-764`).
- Executor gates (per-step mode): refuse dirty target files (`runner/executor.py:74-102`), unexpected-write detection via porcelain diff (`:145-180`), action/status cross-check (`:190-229`), post-conditions (`:416-466`), git-vs-log resume consistency (`:313-391`), no `shell=True` anywhere (test commands go through `shlex.split`, `:704`).
- Environment allowlist for the `claude` subprocess (`runner/claude_subprocess.py:43-101`) — secrets in the parent env do not leak.
- Detached isolation: fresh worktree, `approval` refuses to merge inside a worktree (`runner/approval.py:308-311`), so overnight runs never touch `master` or push.
- Sizing gate (`runner/usher.py`), `max_unreviewed`, per-run token cap, Job Object teardown.

**What is not real, or weaker than it looks:**
- **The LLM's tool permissions are whatever the operator's Claude Code settings allow.** `claude_subprocess.run_claude` passes no `--allowedTools`, `--disallowedTools`, `--permission-mode`, or `--max-turns` (`runner/claude_subprocess.py:142-144`). `.claude/settings.local.json` in this checkout allows `Bash(git push *)` and `Bash(git *)`. In interactive mode the subprocess runs in that checkout and inherits those grants, so a defender or executor call *can* push. In detached mode the worktree only carries the tracked `.claude/settings.json` (6 rules), but the user-level `~/.claude/settings.json` still applies and was not audited.
- **Can a run touch files outside its worktree?** Yes. The write guards are all post-hoc `git status` diffs of the worktree; anything written to `~/.claude`, another repo, or the apiary main checkout (reachable via `..` from `<target>/.runner-worktrees/<x>`) is invisible. `validate_plan`'s allowlist constrains *declared* `step.files`, not what the model does with Bash/Write.
- **Monolithic mode weakens every per-step guard to post-hoc.** Unexpected writes are detected after they are committed (`runner/monolithic_executor.py:289-307`) and the branch is left in place for review; the model is *instructed* to commit per step but nothing enforces it.
- **`approval` pushes.** `git push` with no confirmation on the auto-merge path (`runner/approval.py:99-104,347-363`). This is only reachable interactively, which is exactly when an operator is least expecting an unattended push. Given the user memory "sweep secrets before push" and "/wrapup does not push", this is a policy violation waiting to happen.
- **What stops runaway spend?** Per-call timeouts (`config.json`), `stage_timeout`, the raw-token cap checked between stages, and `max_unreviewed`. Not: the cross-run tracker (dead, Bug 4), the no-usage check on failed stages (Bug 8), or any dollar-denominated limit (the envelope's `total_cost_usd` is ignored by `cost_emit`).
- `harden` findings are validated with `--sanitize` before being fed to the defender (`runner/auto_harden.py:132`), but the defender's free-text `description` flows straight into the approval triage prompt (`runner/approval.py:154-178,183-205`) — an attacker-controlled path from repo content → finding → "accept this deferral" prompt injection → auto-merge.

---

## 5. Code quality

**Five largest functions (AST-measured):**
1. `runner/run.py:471` `_run_detached_impl` — **471 lines**
2. `runner/executor.py:808` `main` — 379 lines
3. `runner/run.py:1232` `main` — 286 lines
4. `runner/approval.py:252` `main` — 238 lines (nesting depth 8 — deepest in the package)
5. `runner/auto_plan.py:110` `build_prompt` — 228 lines (mostly a prompt literal)

Runners-up: `auto_harden.main` 179, `validate_plan.validate` 148, `validate_spec.validate` 131, `monolithic_executor.main` 127. The code-style standard says "Keep functions short and focused. If a function does two things, split it." (`docs/standards/code-style.md`).

**Dead code (verified by repo-wide grep, including `*/commands/*.md`, `scripts/`, `docs/`):**
- `runner/run.py:243-248` `extract_usage_block` — no callers.
- `runner/run.py:35-41` imports `append_overnight_log`, `OVERNIGHT_LOG` — unused in the file.
- `runner/queue.py:6` imports `SCRIPT_DIR` — unused.
- `runner/detached_lib.py:45-47` `short_uuid` — no callers.
- `runner/run_history.py:68-85` `read_entries` — no callers.
- `runner/usher_order.py:53-84,193-211` `register_standalone`, `register_group`, `archive_order` — test-only; no CLI or caller.
- `runner/detached_lib.py:186-205` `stage_review_artifacts` — functionally dead (Bug 10).
- `runner/detached_lib.py:280-314` `prune_stale_worktrees` — scans a directory nothing writes to (Bug 4).
- `runner/abort.py:110-122` `_save_blocker_note` — always returns early (Bug 11).
- `runner/executor.py:698-702` `cd &&` branch — validator makes it unreachable.
- `runner/validate_plan.py:910-916` — `pass` loop.
- `runner/approval.py:315,396` `original_branch` — assigned, never read.
- `runner/cron_setup.md` — five-line tombstone pointing at `scheduling.md`.
- `runner/{executions,plans,specs,hardens,reports}/` — 36 stranded April artifacts in the source tree (gitignored, still misleading).

**Leftovers and inconsistencies:**
- "Chained executor" strings survive the rename in `runner/monolithic_executor.py:312,384,406` and `runner/test_monolithic_executor.py:29` (`ChainedExecutorTestBase`).
- `DEBUG` prints shipped in production (`runner/auto_harden.py:291-292`).
- The planner prompt tells the model that test commands go to `subprocess.run(shell=True)` (`runner/auto_plan.py:201-203`); `validate_plan.py:54-55,281-284,307-308` repeat it. The executor uses `shlex.split` and never `shell=True` (`runner/executor.py:704-716`). The prompt lies to the LLM about the system it is planning for.
- Inline imports mid-function: `import re as _re` (`runner/executor.py:698`), `import re` (`runner/auto_refine.py:184`, `runner/auto_harden.py:104`), `import os` (`runner/schedulers/windows.py:226`), cross-stage `from .auto_plan import _sanitize_json_newlines` (`runner/auto_refine.py:198`), `from .run import _find_runner_branches_from_refs` inside `abort` (`runner/abort.py:67`, a circular-import workaround).
- Module-level side effects at import: `EXECUTIONS_DIR = executions_dir()` (`runner/executor.py:43`), `LOCKS_DIR = locks_dir()` (`runner/run_lock.py:19`), `_EXECUTOR_MODULE` from config (`runner/run.py:78-81`), `REGISTRY_PATH = registry_path_for_host()` (`runner/cron_health.py:69`). These freeze env-dependent paths at import, which is why every test has to `mock.patch.object` them and why the orchestrator tests leak into the real locks dir.
- Runtime state committed to git: `runner/usher_order.json`, `runner/usher_order_archive/`.
- Naming: `runner/detached_lib.py:4` `import json, os, re, shutil, subprocess, sys, uuid as uuid_mod` on one line, `Optional[...]` alongside `X | None` in the same package, `SCRIPT_DIR.parent` vs `REPO_ROOT` vs `REPO_DIR` vs `_REPO_ROOT` vs `APIARY_REPO_ROOT` for the same path.
- Ticket-number archaeology in comments (`#235`, `#236`, `ATK-010`, `T-2026-119`, `#248`, `#249`) is dense enough that the code reads as a changelog; fine for provenance, but many of the referenced tickets are gone from the scribe store.

**No `TODO`/`FIXME`/`HACK` markers in code** (the only hit is the placeholder regex in `validate_spec.py:54`). No commented-out code blocks of note.

---

## 6. Tests

`poetry run pytest runner -q` → **594 passed in 35.47s**. (Note the standard says "Use `unittest`. No pytest" — the files are `unittest` and pytest merely collects them.)

**Well covered (real behaviour, real git repos in tempdirs):**
- `test_executor.py` (88 tests): `commit_files`, `assert_no_unexpected_writes`, `_assert_action_matches_staged`, `validate_resume_state`, `verify_post_conditions`, `parse_verify_output`, `files_touched_by_prior_steps`, retry/subsumption flow, `persist_execution_log`. This is the best test file in the package.
- `test_validate_plan.py` (81): every validator check, including gitignore via real `git check-ignore`.
- `test_monolithic_executor.py` (8): reconstruction from real commits.
- `test_usher_order.py`, `test_usher.py`, `test_files_examined.py`, `test_run_lock.py`, `test_schedulers_windows.py` (CSV/XML/time parsing), `test_cron_health.py`.

**Mock-heavy or hollow:**
- `test_run_detached.py` (49 tests, 225 mock/patch references): `git_worktree_create`, `prune_stale_worktrees`, `run_stage` and `git_commit_all_in` are mocked in every scenario. It cannot see Bugs 3, 4, 10 or the branch/worktree interaction that is the whole point of detached mode. `test_git_setup_failed` asserts the early-return path that is itself Bug 4.
- `test_orchestrator.py` (36): `run_stage` fully mocked for `main()`; nothing chains two real stage scripts. Also writes to the real `.apiary/runner/locks/` (no `LOCKS_DIR` patch), so it is not hermetic and depends on the machine having no stale lock.
- `test_abort.py`: patches `SCRIPT_DIR` to a tempdir, thereby codifying the stale `runner/specs/` archive path (Bug 11).
- `test_detached_lib.py::stage_review_artifacts`: plants the files the runner never writes (Bug 10).

**Not covered at all** (grep of test files): `approval.py` — zero tests reference the module (squash merge, push, deferral review, verdict routing); `auto_harden.main` (only `compute_verdict` and `load_harden_verdict`); `auto_refine.extract_spec` / `auto_plan.extract_plan` (the JSON-salvage parsers that every run depends on); `run_cleanup`, `run_prune_failed`, `_signal_handler`, `_install_kill_on_job_close`; `build_monolithic_prompt`, `reconstruct_step_results` directly; `claude_subprocess.run_claude`'s timeout/OSError branches (only `_build_subprocess_env` is tested); the `schema_version` producer/consumer pairing across stages; any multi-repo path through `validate_plan`.

There is no end-to-end test that runs two real stage subprocesses back-to-back with a fake `claude` binary. That single test would have caught Bug 1 on the day it was introduced.

---

## 7. Docs vs reality

| Doc | Claim | Reality |
|---|---|---|
| `README.md:166` | executor "makes the code changes, committing per step" | Default is monolithic: one subprocess, commits are requested from the model and reconstructed post hoc (`runner/config.json:17`, `runner/monolithic_executor.py:190-286`). |
| `README.md:168`, `:181` | approval "auto-merges clean runs" | Never in detached mode (`worktree-deferred`, `runner/approval.py:308-311`); only interactively, where it also pushes. |
| `README.md:170,176`, `runner/scheduling.md:10-16`, `docs/reference/cli-tools.md:594-735`, `runner/commands/runner-prep.md` ("write to `runner/intake/`", "validate `runner/intake/<uuid>.json`") | artifacts live under `runner/intake/`, `runner/specs/`, … | They live under `<state>/runner/` or `<target>/.apiary/runner/` since `e887b17` (`runner/target_repo.py:114-144`). `/runner-prep` would write into a directory `run.py --detached` never reads. |
| `README.md:184,321-322` | registry is `runner/cron_registry.json` | `cron_registry/<hostname>.json` at repo root (`runner/cron_health.py:48,61-63`); cli-tools.md:843 has it right. |
| `README.md:329-334` | `backlog/`, `intake/` directories under `runner/` | Do not exist in the source tree. |
| `README.md:180` | "any other zero-usage stage aborts the run so token caps can't be bypassed" | Only when the stage exits 0 (`runner/run.py:835-843`); failed/timed-out stages record 0 tokens silently. |
| `runner/scheduling.md:41,119` | `detached.token_cap` default 2,000,000 | `runner/config.json:32` says 10,000,000. |
| `runner/scheduling.md:47-73` "Path A: Remote trigger (recommended)" | run `python -m runner.run --detached` from a cloud `/schedule` routine | The backlog, state dir, `claude` auth and target git repo are all local disk; a cloud routine has no backlog to pick from. Unverified, but there is no mechanism described that would make it work. |
| `runner/scheduling.md:130` | `python -m runner.queue` lists branches joined with `overnight.jsonl` | Join key mismatch → every column `unknown` (Bug 3). `queue.py` is also absent from `docs/reference/cli-tools.md`. |
| `docs/reference/cli-tools.md:616-618` | multi-repo: artifacts under `runner/<dir>/`, work lands "on a `runner/<slug>-<uuid>` branch" | Artifacts under the state dir; code commits land on `runner/<uuid>` (Bug 3); plan validation fails for non-apiary targets (Bug 2). |
| `docs/reference/cli-tools.md:744` | executor "Fails if branch `runner/<uuid>` already exists (not idempotent)" | It checks the branch out and resumes (`runner/executor.py:862-873`). |
| `docs/reference/cli-tools.md` | no entries for `runner/queue.py`, `runner/usher.py`, `runner/monolithic_executor.py`, `runner/run.py --abort` semantics beyond one line | These are user-invocable CLIs. |
| `docs/standards/schema-migration.md:37` | in-flight artifacts under `runner/plans/` etc. | Same path drift. |
| `runner/approval.py:9` | "No LLM calls — purely mechanical" | `review_deferrals` calls Claude (`runner/approval.py:181-224`). |
| `runner/run.py:859-863` | review artifacts are force-staged so they ride along on merge | They are not (Bug 10). |
| `runner/hooks/post-merge:9` | "Installed by: python setup.py --global" | `--global` was removed; installed by `scripts/install_repo_hooks.py:80-93`. |
| `runner/cron_health.py:3-4,21-22,413-416` | registry at `runner/cron_registry.json`; invoke via `~/.claude/apiary_launch.py` | Wrong path; the global launcher no longer exists (per-repo `.claude/apiary/launch.py`). |
| `runner/auto_plan.py:201-203`, `validate_plan.py:54,281,307` | "The executor passes code_spec directly to subprocess.run(shell=True)" | It uses `shlex.split` (`runner/executor.py:704-716`). |
| `runner/claude_subprocess.py:103-105` | 50 MB cap "prevents unbounded memory growth" | Truncation happens after full capture. |
| `runner/run_tracker.py:89-93` docstring | "Review artifacts live under `<apiary>/.apiary/runner/` today" | Or `$APIARY_TARGET_STATE_DIR/runner/`; the launcher path wins. |

---

## 8. Verdicts

| Component | Verdict | Reason |
|---|---|---|
| `run.py` orchestrator core (`run_stage`, cost scraping, `STAGES`) | keep / improve | Sound loop; split `_run_detached_impl` (471 lines) and `main` (286), fix encoding, unify the skip-entry literal. |
| `run.py` detached branch/worktree flow | rewrite | Two-branch split, dead resume, prune dir mismatch — the branch model needs one owner. |
| `validate_intake.py`, `validate_spec.py` | keep | Small, deterministic, tested. |
| `validate_plan.py` | improve | Excellent checks; must take a repo root parameter instead of apiary's (Bug 2); delete the `pass` loop and the `shell=True` docstrings. |
| `auto_refine.py`, `auto_plan.py` | improve | Merge the two retry loops and five JSON-salvage parsers into one `stage_lib`; fix the prompt's `shell=True` claim. |
| `executor.py` (per-step) | keep | Best-engineered and best-tested module; keep as the default until monolithic earns it. |
| `monolithic_executor.py` | improve | Add `schema_version` and plan assertion; its post-hoc guarantees are weaker and it has never produced a real artifact — not a safe default yet. |
| `auto_harden.py` | improve | Remove `git add -A`, DEBUG prints, sys.exit inside try; align `MAX_ROUNDS` default; add tests for `main`. |
| `approval.py` | rewrite | Duplicated 55-line merge blocks, unused vars, untested, pushes without consent, notes go to the wrong store. Remove `push()` entirely. |
| `claude_subprocess.py`, `cost_emit.py` | improve | Add explicit tool/permission and turn limits; emit usage on non-zero exit when an envelope exists. |
| `git_lib.py` | improve | Finish the consolidation it was created for (`branch_exists`, `checkout`, `current_branch`), add `encoding="utf-8"`. |
| `detached_lib.py` | improve | Fix `worktrees_dir_for` vs `worktrees_dir`; delete `stage_review_artifacts`, `short_uuid`; parameterise cwd. |
| `target_repo.py` | keep | Clear precedence, decent tests. |
| `run_lock.py` | improve | Refresh `started_at` on `update()`; PID probe is well done. |
| `run_tracker.py` | delete (or make reachable) | Unreachable in production; if kept, resume must check `valid`/`status`. |
| `abort.py` | improve | Point at `artifacts_root()`; drop the global-launcher note or route through the per-repo launcher. |
| `queue.py` | improve | Join by UUID (both branch names contain it), not by branch string. |
| `usher.py` | keep | Small pure function, tested. |
| `usher_order.py` | delete | No CLI, no producer, one hand-written manifest from April, duplicates the backlog's job. |
| `run_history.py` | improve | Drop the `overnight.jsonl` double-write and `read_entries`; one log. |
| `cron_health.py` + `schedulers/` | keep | Works as scoped, honest about Windows-only; fix stale docstrings/hints. |
| `draft_ticket.py`, `promote.py`, `mark_done.py`, `create_intake.py`, `refine_to_intake.py` | improve | Five CLIs with three `slugify`s and three `read_todo`s; collapse into one `ticket.py` with subcommands. |
| `close_source_todo.py` + `hooks/post-merge` | keep | Small, tested, does one thing; fix the header comment. |
| `commands/runner-prep.md` | improve | Points at `runner/intake/`; must use the launcher/state path. |
| `scheduling.md`, `cron_setup.md` | rewrite / delete | Path A is unsubstantiated; defaults wrong; `cron_setup.md` is a tombstone. |
| `runner/{executions,plans,specs,hardens,reports}/`, `usher_order*.json` | delete | Stranded runtime state in the source tree. |

---

## 9. Top 10 recommended changes (ranked by value ÷ effort)

1. **Stamp `schema_version` (and assert the plan's) in `monolithic_executor`** — unblocks every default-mode run. Add one test that feeds a monolithic execution log into `auto_harden.main`. **S**
2. **Add a hermetic end-to-end test with a fake `claude` executable on `PATH`** that drives `run.main()` through all six real stage scripts in a tempdir target repo. This is the test that would have caught Bugs 1, 3, 10 and 12. **M**
3. **One branch per run.** Pass the branch name into stage 4 (env var or plan field) so the executor works on the worktree's existing branch instead of `checkout -b runner/<uuid>`; then `queue.py` joins correctly and `max_unreviewed` counts one branch per run. **M**
4. **Make `validate_plan` take the repo root from the plan's `target_repo` (falling back to apiary)** and give `detached_lib._git` a mandatory `cwd`. Without this, `--target-repo` is advertised but broken. **M**
5. **Remove `git push` from `approval.py` and `git add -A` from `auto_harden.py`**; pass explicit `--allowedTools`/`--disallowedTools` (deny `git push`, deny writes outside cwd) and a `--max-turns` to `claude -p` in `claude_subprocess`. Cheapest safety win in the package. **S**
6. **Fix the worktree directory mismatch and make failure recovery a decision, not an accident**: either auto-remove the preserved worktree on the next run when the branch still exists (the branch keeps the work), or record the attempt and mark the ticket failed before returning `git_setup_failed`. Then delete `run_tracker` or make `get_resume_stage` honour `valid`/`status`. **M**
7. **`encoding="utf-8"` on every `text=True` subprocess and every `NamedTemporaryFile(mode="w")`** in `runner/` (≈15 sites); the repo standard already mandates it. **S**
8. **Create `runner/stage_lib.py`** with one `run_claude(stage)`, one `extract_json(text, want_keys)`, one `retry_until_valid(build_prompt, validator)`, and one `check_uuid_safe()`; delete the 5/5/6 copies. Also finish `git_lib` (`branch_exists` with `refs/heads/`, `checkout`, `current_branch`). **M**
9. **Emit usage on non-zero exits when an envelope is present, and count timed-out calls against the cap** (`claude_subprocess` + `record_stage_cost`), and refresh `started_at` in `run_lock.update()` so long runs are not "stale". **S**
10. **Docs and tree hygiene pass**: correct artifact paths (README, cli-tools, scheduling.md, runner-prep.md, schema-migration.md), delete `cron_setup.md` and Path A, remove stranded `runner/{executions,…}` and `usher_order*.json`, fix `post-merge`/`cron_health` launcher references, add `queue.py`/`usher.py`/`monolithic_executor.py` to cli-tools. **S**

Deferred but worth deciding: whether `monolithic` should remain the default executor at all. The per-step executor is the module with real guarantees and real tests; the monolithic one trades every mid-run invariant for token savings that have never been measured on a completed run.
