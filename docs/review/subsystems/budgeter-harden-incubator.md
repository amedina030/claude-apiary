---
type: architecture
title: "Budgeter, harden, incubator review"
scope: project
description: Deep review of budgeter hooks/estimator, harden validators and prompts, incubator spawn (2026-08-26 deep review, subsystem appendix)
framework_version: "1.0"
last_verified: 2026-08-26
---

> **Snapshot of 2026-08-26; superseded by the remediation — see CHANGELOG. Deleted at close-out (T-2026-271).**

# Review: budgeter / harden / incubator

Read-only staff review of three subsystems in `D:\Professional\claude-apiary` (2026-08-26, HEAD `1bee5e5`). Every non-test `.py`, every `commands/*.md` and `agents/*.md`, `docs/architecture/hook-lifecycle.md`, `docs/standards/code-style.md`, and the relevant README / `docs/reference` sections were read in full. Claims were verified by reading code, by grepping the whole repo for callers, by inspecting real data in `budgeter/data/` (26,507 log entries, 3,763 feedback records, 886 sessions, 2026-04-02 → 2026-08-26) and real Claude Code transcripts under `~/.claude/projects/`, and by running the existing tests (`poetry run pytest budgeter harden incubator -q` → **151 passed in 22s**). Two hook-crash reproductions were run against a throwaway project in the scratchpad with `APIARY_BUDGETER_TEST_ISOLATION=1`; nothing in the repo was mutated.

---

## 1. What it is

### Budgeter (`budgeter/`, ~3.5k lines)
Token-usage accounting for Claude Code sessions, implemented as three shell hooks. `hooks/pre_tool_use.py` fires before every monitored tool call (`Agent`, `Bash`, `Read`, `Write` per `config.json:2-7`), re-parses the session transcript JSONL, computes the token delta since the previous PRE, and appends a log line for the *previous* tool to `budgeter/data/usage_log.jsonl`. `hooks/post_tool_use.py` logs subagent cost from `tool_response.totalTokens` on `Agent` calls. `hooks/stop_session.py` logs the last tool of the turn and deletes the per-session baseline. `lib/logger.py` owns file paths, locking, transcript parsing and baseline files; `lib/estimator.py` is a keyword/regex "is this task going to be expensive" heuristic plus a median-of-similar-tasks magnitude estimate; `report.py` and `tune.py` read the JSONL; `log_agent_cost.py` / `query_request.py` are CLI shims used by runner and `/harden`. Four toggle skills flip feature flags.

### Harden (`harden/`, ~2.9k lines)
An LLM-orchestrated attack/defend loop. The 747-line `commands/harden.md` skill is the actual program: it spawns N read-only "lens" attacker agents (7-lens taxonomy in `lenses.py`), a consolidator/referee that dedups and adjudicates, and one persistent Defender agent that edits code in a git worktree. The Python (`validate_findings.py`, `validate_consolidation.py`, `validate_response.py`, `validate_and_assign.py`, `assign_ids.py`, `round_counter.py`, `validate_common.py`) is schema validation + deterministic ID stamping for the JSON the agents emit, plus a tiny per-session round counter. Budget tracking piggybacks on budgeter by tagging agent descriptions with `[rid:<request_id>]`.

### Incubator (`incubator/`, ~1.1k lines incl. tests)
`commands/incubator.md` orchestrates `/refine` → ask for a path → `cli.py spawn` → `cli.py verify`. `cli.py spawn` validates the target path, `git init`s it, renders three templates (`.gitignore`, `pyproject.toml`, `CLAUDE.md`), runs the per-repo apiary install (`core.install.install`), installs the secret-scan pre-commit hook, and migrates the refine spec from apiary's scribe into the new repo's scribe (via the new repo's launcher) then closes the original. `cli.py verify` re-checks the six artefacts a spawn must produce.

---

## 2. Architecture assessment

### Budgeter — does PRE-to-PRE actually measure what it claims?

**What it reads.** `logger.read_session_jsonl` (`logger.py:252`) re-reads the *entire* main-session transcript on every monitored tool call, then `get_cumulative_tokens` (`logger.py:341-351`) sums `input_tokens + output_tokens + cache_read_input_tokens` over every line whose `message.role == "assistant"`. The delta between consecutive PREs is written as `tokens_delta`; a second figure `net_tokens_delta` is `max(0, Δinput) + max(0, Δcache_read) + last_output` (`pre_tool_use.py:163-171`).

**What it gets wrong — verified against real transcripts:**

1. **Multi-block assistant messages are written as multiple JSONL lines that each repeat the same `usage` object.** In transcript `973fbf33…jsonl` (6.2 MB) there are 685 assistant lines but only 384 distinct `message.id`s; all 228 multi-line messages carry byte-identical `usage` across their lines. `get_cumulative_tokens` therefore counts a thinking+text+tool_use message three times. `tokens_delta` — the "gross cost" that `query_request.py` sums for `/harden`'s budget (`lib/query.py:20`) and that `report.py` uses to filter zero entries — is inflated 2-3× for every multi-block turn. `net_tokens_delta` is unaffected because `get_last_call_tokens` (`logger.py:354`) only keeps the last line.
2. **`cache_creation_input_tokens` is ignored entirely** (`logger.py:348-350`, `354-366`). Real usage from this session: `{"input_tokens": 2, "cache_creation_input_tokens": 50855, "cache_read_input_tokens": 0, "output_tokens": 1239}` — the hook would record that call as 1,241 tokens. Cache creation is the *most expensive* input category (1.25× base rate). Across `973fbf33` it was 4.8M tokens vs 1.4k plain input. `session_length_nudge` (`pre_tool_use.py:276`, fed `last_input + last_cache`) has the same blind spot: on a cache-miss call it sees "2 tokens of context".
3. **Parallel tool calls produce phantom entries.** When one assistant message issues N tool_use blocks, the N PREs fire back-to-back with no API call between them; `tokens_delta` is 0 but `net_tokens_delta = 0 + 0 + last_output` (`pre_tool_use.py:171`) so `append_entry`'s zero-filter (`logger.py:180`) does not drop them and `last_output` is re-attributed N-1 extra times. Real data: **6,326 of 25,062 non-agent entries (25%) have `tokens_delta == 0` and `net_tokens_delta > 0`**, contributing 5.86M of 116.6M net tokens. `report.py` hides them by default (`report.py:471`, filters `tokens_delta != 0`), but `estimator._group_tasks`, `tune.py` and `report.py --feedback` use the unfiltered `read_log()` and count them.
4. **Off-by-one attribution is by design and only half-true.** The cost logged for "tool N" is actually the API call that *consumed tool N's result and decided on tool N+1* — i.e. it includes the reasoning for N+1 and any text emitted before N+1. For a `Read` followed by a long explanation and then a `Write`, the explanation's output tokens are attributed to the `Read`. The doc's claim ("the true cost of the previous tool call", `pre_tool_use.py:4-7`) is a simplification.
5. **Thinking tokens** are inside `output_tokens` (`output_tokens_details.thinking_tokens` is a sub-field), so they are counted — correctly.
6. **Subagents** are not in the main transcript (0 `isSidechain` entries in the three most recent transcripts), so the doc's reasoning at `hook-lifecycle.md:31-37` is right that PRE-to-PRE cannot see them. `post_tool_use.py` covers foreground `Agent` calls from `tool_response.totalTokens` — but see §3 for the 64 KB stdin cap that silently drops large agent payloads, and note that the skip-if-previous-was-Agent guard (`pre_tool_use.py:161`) means a dropped PostToolUse entry is lost forever, not recovered.

**The `[CONT]` chaining pattern is effectively dead in production.** `hook-lifecycle.md:41-49` describes inheriting `task_turn` across a clarifying question. That requires the baseline file to survive between turns. But `stop_session.py:96` calls `cleanup_session` unconditionally at the end of *every* assistant turn (the `Stop` hook fires per response, not per session — the feedback file shows a mean of 4.6 and max of 62 records per session, exactly one per turn). At the next turn the baseline is `None`, so `pre_tool_use.py:79-86` falls through to `task_turn = turn_number`. Real data: **only 11 of 25,027 non-agent entries have `task_turn != turn_number`**. Likewise the "approval inheritance" branch (`pre_tool_use.py:104-115`) and the PRE-side feedback write at task boundaries (`pre_tool_use.py:199-210`) require a surviving baseline and never fire. The `_CONT_INSTRUCTION` (`pre_tool_use.py:26-32`) is still injected into every session's context and asks Claude to prefix messages with `[CONT]` for no effect.

**Is the "expensive task" estimator a sound predictor?** No. It is five rules (`estimator.py:78-121`): keyword sets on the *first assistant text of the turn* (which at first-tool-call time is usually one sentence), a regex count of filenames, a count of `then/next/also/first/finally`, and investigative words in the user prompt. Real feedback (`report.py --feedback`, 3,717 tasks): overall warning precision **5/53 = 9%**; the base rate of "expensive" (≥75th percentile by construction) is 25%, and every rule sits at 26-41% precision — `file_count` 41%, `investigative_keywords` 32%, `step_count` 30%, `scope_keywords` 26%. Entries with *no* rules hit have 23% precision. The rules are barely distinguishable from the base rate, and the one combination that reaches 80% (`file_count + investigative_keywords + step_count`) has n=5. Structurally the estimator cannot work: cost is dominated by how many tools Claude ends up calling, which is not visible in the opening sentence. `warn_score_threshold: 1.0` with `investigative_keywords` weight 1.0 (`config.json:24-31`) means any user prompt containing "why" or "explain" trips the warning once 10 flagged tasks exist — which is presumably why `budgeter-warn` is off (only `budgeter-log` and `budgeter-session-warn` flags exist in `.claude/apiary/flags/`).

**Has it been tuned against real data?** `tune.py` exists and was run once at commit `08928bc` (2026-04-01, "lower warn threshold to 1.0, disable breadth_keywords"), when the log had days of data. Running it today proposes `file_count 1.6→2.0`, `scope_keywords 0.8→0.7`, others unchanged — i.e. noise within the clamp. The tuner scales weights by precision relative to the mean of precisions (`tune.py:129-160`), which cannot fix a signal that is not there; it also treats the *feedback* records' `scope_flags` as ground truth even though 25% of the cost it is fitting against is phantom (item 3 above).

**Latency.** Per monitored tool call: one Python start for the launcher (`.claude/apiary/launch.py`) + a second for the hook (`subprocess.run([sys.executable, script])`, launcher line ~100) + full transcript parse. Parsing is cheap (6.2 MB in 0.05 s); interpreter start on Windows is ~80-150 ms each, so ~200-300 ms of overhead per `Read`/`Bash`/`Write`/`Agent`. The warn path additionally parses the 70 MB `usage_log.jsonl` under a lock (0.36 s measured) once per user turn.

### Harden — well-specified, and is the Python earning its keep?

The flow is specified in unusual detail and the specification is mostly internally consistent: path selection (`harden.md:31-39`), per-round routing (`harden.md:284-292`), retry-once-then-degrade for the consolidator (`harden.md:375-382`), mechanical `prior_record` construction (`harden.md:605`), and a strict "nothing mutates before Step 1.5" rule (`harden.md:179`). The Python layer is **real work, not ceremony**: the validators reject the exact failure modes an LLM produces (invented `id`s, missing `source_ids`, refs not covered, duplicate refs, non-string fields, multi-file locations), `validate_consolidation.validate` enforces *exactly-once* coverage of every dispatched finding (`validate_consolidation.py:139-149`), `degrade_dedup` gives a deterministic fallback, and `assign_ids` keeps IDs out of the model's hands. That is precisely the "LLM proposes, code disposes" contract that makes the loop debuggable. Weak spots: the retry loop is by prose only (no retry counter in code — a validator that keeps failing can loop as long as the model keeps re-spawning); `check_path_escape` (`validate_common.py:53-64`) checks against `Path.cwd()`, not the target repo, so it is only as good as the launcher's cwd; and `degrade_dedup` keys on the raw `location` string (`validate_consolidation.py:185-190`) so `a.py:10` and `a.py:10-12` never merge.

The budget contract has a hole: `harden.md:533` says round-2+ Defender `SendMessage` continuations "land on the parent session bucket" and "the run still aborts on overrun via the parent-session fallback path". There is no such path — `query.total_tokens_for_request` (`lib/query.py:20`) sums only entries whose `request_id` matches. Continuation tokens are simply invisible to the budget check.

### Incubator — sound or thin?

It is more than `git init` + `apiary install`, but not by much: path validation with a nested-repo guard (`cli.py:99-124`), templated skeleton (`cli.py:172-200`), install-before-migrate ordering (`cli.py:377-392`), best-effort hook install, and a `verify` subcommand that exists because a prior session hand-authored the files instead of running the CLI (`cli.py:203-215` — the docstring tells the story). The exit-code contract and rollback-on-early-failure are correct. The remaining risk is the spec migration (§3).

---

## 3. Bugs and correctness risks (ordered by severity)

### B0 — CRITICAL (shared with core): budgeter answers every monitored tool call with `permissionDecision: "allow"`
`pre_tool_use.py:55` and `:288` call `hook_allow()` (`core/hook_context.py:43`), which prints `{"hookSpecificOutput": {"permissionDecision": "allow"}}` for every `Bash`, `Read`, `Write` and `Agent` call. Per the Claude Code hooks guide (confirmed by the docs agent, see Addendum): a hook `"allow"` **bypasses the interactive permission prompt**; across multiple hooks the most restrictive decision wins in the order `deny > defer > ask > allow`; and hook `allow` cannot override settings *deny rules*. This repo has **zero** deny rules in `.claude/settings.json` or `~/.claude/settings.json`, and every other core PreToolUse hook also emits `allow` (`check_install.py`, `inject_session.py`, `startup_hook.py`, `learnings_inject_hook.py`, `research_capture_reminder.py`, `pre_push_*` — grepped). Net effect, as documented: in every bootstrapped repo, no `Bash` command is ever gated by the normal "allow this command?" prompt (Claude Code's built-in protections that hooks cannot loosen — e.g. connector/MCP `ask` settings — still apply, which is consistent with the user still seeing *some* prompts). The intent (commit `00bce9d`, 2026-04-03) was only to stop "intermittent hook error messages" from bare `sys.exit(0)`; the fix should have been to print `{}` or omit `permissionDecision`, which lets the hook add `additionalContext` without voting on permission. This is a one-line change in `core/hook_context.hook_allow` (outside this cluster) but budgeter is its heaviest user, so it is reported here. **Recommend confirming empirically in a bootstrapped repo before/after removing the key.**

### B1 — HIGH: a corrupt baseline file wedges the session's hooks permanently
`pre_tool_use.py:43-288` has no try/except around `main()`, contrary to `docs/standards/code-style.md` ("Hooks must not crash. Wrap the entire `main()` in a try/except"). `save_baseline` writes in place with `open(path, "w")` (`logger.py:412`) — not atomic — so a hook killed mid-write leaves truncated JSON. On the next PRE, `load_baseline` (`logger.py:404`) raises `JSONDecodeError` *before* any flag check or `save_baseline`, so the file is never rewritten. **Reproduced in the scratchpad:** baseline `{"tokens": 12` → `pre_tool_use.py` exits 1 with a traceback; `stop_session.py` with `budgeter-log` enabled also exits 1 at `stop_session.py:38` before reaching `cleanup_session` at `:96`, so the corrupt file persists. Every subsequent monitored tool call in that session emits a hook error and logs nothing. (`post_tool_use.py:67` has the same unguarded `load_baseline`.) Failing input: any non-JSON `budgeter/tmp/<sid>_baseline.json`.

### B2 — HIGH: `tokens_delta` over-counts multi-block assistant messages 2-3×
`logger.get_cumulative_tokens` (`logger.py:341-351`) sums usage per JSONL line, but Claude Code writes one line per content block with the same `usage` (verified: 228/384 messages multi-line, 100% identical usage). Wrong outcome: `/harden`'s budget gate and `report.py`'s gross totals are inflated for every turn that includes thinking or text alongside a tool call. Fix is a one-liner: dedupe on `message.id`.

### B3 — HIGH: parallel tool calls create phantom `net_tokens_delta` entries
`pre_tool_use.py:171` adds `last_output` even when no API call happened between PREs (`tokens_delta == 0`). 6,326 real entries (25%) are phantoms totalling 5.86M tokens. They feed `estimate_magnitude`, `tune.py` and `--feedback`. Failing state: any assistant message with ≥2 `tool_use` blocks. Fix: if `tokens_now == baseline["tokens"]`, log nothing (or log with `net_tokens_delta = 0`).

### B4 — HIGH: `/budgeter-log`, `/budgeter-warn`, `/budgeter-session-warn` toggle the wrong file
The three skills run `rm/echo ~/.claude/budgeter-<x>-enabled` (`budgeter-log.md:11`, `budgeter-warn.md:11`, `budgeter-session-warn.md:11`), but since commit `2149090` (2026-05-05) `core/flags.py:_flag_path` reads `<repo>/.claude/apiary/flags/<flag>-enabled`. The skill files were last touched at `009c367` (2026-03-14). Wrong outcome: user runs `/budgeter-warn`, sees "ON", nothing changes; `~/.claude/budgeter-*-enabled` does not exist on this machine while `.claude/apiary/flags/budgeter-log-enabled` does. The installed copies in `.claude/commands/` are byte-identical to the sources, so this is not an install-drift issue.

### B5 — HIGH: `cache_creation_input_tokens` is never counted
`logger.py:348-350` and `:354-366`. Wrong outcome: a call that writes a 50k-token cache is logged as ~1k tokens; `session_length_nudge` can read a full context as nearly empty on a cache-miss turn. Data in §2.

### B6 — MEDIUM-HIGH: `post_tool_use.py` silently drops Agent payloads over 64 KB
`post_tool_use.py:22,45` reads at most 65,536 bytes of stdin then `json.loads` → `JSONDecodeError` → `sys.exit(0)` with no log line. The PostToolUse payload contains `tool_input.prompt` (harden prompts are 3-4 KB of template + findings JSON + target list) **and** `tool_response` (the agent's full return text; a lens attacker returning 30 findings with `scenario` strings, or a research agent returning a report, is easily > 60 KB). Wrong outcome: exactly the most expensive agent calls are the ones that go unlogged, and `/harden`'s budget check under-counts them. The `stop_session.py:21` cap is harmless (Stop payloads are small).

### B7 — MEDIUM: `[CONT]` chaining, approval inheritance and PRE-side feedback never fire (see §2)
Not a crash but a correctness gap between doc and behaviour: `stop_session.py:96` deletes the baseline every turn. The 11 chained entries in 25k prove the code path is reachable only in edge cases (probably turns where the Stop hook did not run). `_CONT_INSTRUCTION` still costs context on every session.

### B8 — MEDIUM: stale `.lock` files add a 2-second stall to every monitored tool call
`_file_lock` (`logger.py:52-81`) uses exclusive-create of `<file>.lock` and waits up to 2 s before proceeding unlocked. There is no mtime-based staleness check and no PID; a process killed while holding the lock (e.g. `taskkill` during a long `read_log()` on the warn path) leaves the lock forever. Every subsequent `append_entry`, `read_log`, `load_baseline`, `save_baseline` then sleeps the full 2 s — 4× per PRE (`load_baseline`, `append_entry`, `save_baseline`, plus `read_log` on the warn path). No stale lock exists today, but nothing prevents it.

### B9 — MEDIUM: `incubator/_migrate_spec` passes the whole spec on argv
`cli.py:331` builds `["add", "--type", "context", "--content", spec_content]`. Windows `CreateProcess` caps the command line at 32,767 chars; a long `/refine` spec (they routinely run several KB, and `scribe/notes.py` already offers `--content-file` at `notes.py:1434`) will make `subprocess.run` raise `OSError` inside `_migrate_spec`, which is *not* caught — the CLI dies with a traceback instead of the documented exit 5, after the repo, install and hooks are already in place. The recovery text at `cli.py:399-407` tells the user to re-run the same argv-based command.

### B10 — MEDIUM: incubator recovery hint duplicates the spec on partial failure
`_migrate_spec` runs `add` in the new repo, then `done` in apiary (`cli.py:333-345`). If `done` fails, the function returns `False` and `cmd_spawn` prints "re-run … `scribe/notes.py add …`" (`cli.py:402-406`) — re-running `add` creates a second copy in the new repo. The hint should be "close the original" only.

### B11 — MEDIUM: `estimate_magnitude` / `_group_tasks` legitimately silently mis-aggregate
`_group_tasks` (`estimator.py:123-161`) keys by `(session_id, task_turn)` and sums `net_tokens_delta`, so it inherits B3's phantoms and B7's non-chaining; it also skips `Agent` entries with `task_turn == 0` (background) but keeps foreground Agent entries whose cost is the *subagent's* total — mixing per-call and per-subagent units in one median.

### B12 — LOW: compaction detection is unverifiable and has fired once in five months
`pre_tool_use.py:138-140` flags compaction when the cumulative sum drops. A JSONL transcript is append-only, so this can only trigger if Claude Code replaces the file; one `[compaction]` marker exists in 26,507 entries. Either compaction never rewrites (and the branch is dead) or it does and the branch under-fires. Unknown; worth an explicit test against a real compacted transcript.

### B13 — LOW: `report.py` and `tune.py` cannot see per-project logs
`logger.configure_for_project` redirects hook writes to `<project>/.claude/budgeter-log.jsonl` when `.claude/budgeter.json` exists (`logger.py:99-134`), but `report.py:20-21` and `tune.py:23-25` hard-code `budgeter/data/*.jsonl` and accept no `--cwd`. `query_request.py` does accept `--cwd`. Inconsistent.

### B14 — LOW: `harden/round_counter.py reset` never deletes state
`cmd_reset` (`round_counter.py:56-58`) writes `{"count": 0}`; `harden/tmp/` holds five `round_*.json` from April-June plus two leftover findings/response temp files. Trivial, but the skill promises cleanup.

### B15 — LOW: `stop_session.py` feedback dedup is per-turn, so it always writes
`append_feedback_if_not_present` keys on `(session_id, task_turn)` (`logger.py:199-231`) — because `task_turn == turn_number` every turn (B7), a record is written for every turn that had a monitored tool. 3,763 feedback rows for 886 sessions; 2,875 have no flags at all. Harmless but explains the file's growth.

### Things checked and found fine
- Baseline files are per-session (`SessionId.tmp_path`), so concurrent sessions do not race on them; concurrent appends to the shared JSONL go through the lock (modulo B8).
- Windows: all file I/O passes `encoding="utf-8"`; `_resolve_filepath` handles `D:/…:45-50`; `cleanup_session` swallows `PermissionError` (`logger.py:438`).
- `round_counter._state_path` sanitises separators (`round_counter.py:24`); the `round_` prefix prevents traversal.
- `validate_response` rejects duplicate `finding_ref`s and unaddressed IDs; `validate_consolidation` rejects invented source IDs.
- No negative `tokens_delta` in 26,507 entries (the `max(0, …)` guards work).

---

## 4. Safety

**Budgeter runs on every tool call.** Cost is ~200-300 ms/call (two Python starts) — acceptable but not free; `Read` is the most frequent tool and is monitored. The hooks are *supposed* to be fail-open but B1 shows they are not: an unguarded exception exits 1. Per the hooks guide, a non-zero exit other than 2 is a **non-blocking error** — the tool proceeds, the user sees the error — and the default command-hook timeout is 10 minutes, so a hung hook (e.g. a stale lock plus a slow disk) would be far worse than a crashed one. There is no path by which budgeter *blocks* a tool.

**The opposite problem is the real safety issue: budgeter *auto-approves* every monitored tool call (B0).** `pre_tool_use.py:55,288` emit `permissionDecision: "allow"`, which the hooks guide defines as bypassing the interactive permission prompt; the most-restrictive-wins rule across hooks does not help because every core PreToolUse hook also says `allow`, and there are no settings deny rules to fall back on. Budgeter only wants to inject `additionalContext`; it should not vote. The one-line fix lives in `core/hook_context.hook_allow` (drop the `permissionDecision` key when no decision is intended), and the change should be verified empirically in a bootstrapped repo.

**Harden isolation is by instruction, not by mechanism.** Attackers and the consolidator are spawned as `general-purpose` agents with full tool access and told they are read-only (`harden.md:313,357`; `attacker_lens.md` "You are READ-ONLY"). The Defender is told to edit at worktree paths and explicitly *not* given `isolation: "worktree"` (`harden.md:488`) — the worktree at `.claude/worktrees/harden-<sid>` is created by a shell command (`harden.md:261`) and nothing stops the Defender from editing the original path if it misreads "use ORIGINAL relative paths in your JSON" as "edit the original files". The worktree-readiness check (`harden.md:240-248`) refuses to run on uncommitted targets, which is a good guard. On Approve the worktree and branch are left in place forever (`harden.md:744` "merge when ready"); on Discard the worktree is removed but the branch is not (`harden.md:651`).

**Incubator writes outside the repo.** Guardrails are real: absolute path required, must not exist, parent must exist, parent must not be inside a git repo (`cli.py:99-124`), `mkdir(exist_ok=False)` (`cli.py:367`), and rollback only removes a directory the CLI itself created (`cli.py:347-349`, `rmtree(ignore_errors=True)`). Nothing is deleted that pre-existed. The one write to a *shared* location is the registry entry via `core.install.install` — not reviewed here, but `verify_spawn` reads it via a private helper (`state._find_entry_by_path`, `cli.py:235`).

---

## 5. Code quality

**Five largest functions** (AST-measured, non-test):

| Lines | Function |
|---|---|
| 246 | `budgeter/hooks/pre_tool_use.py:43` `main` |
| 115 | `harden/validate_response.py:30` `validate` |
| 110 | `harden/validate_consolidation.py:60` `validate` |
| 99 | `harden/validate_findings.py:118` `validate` |
| 89 | `incubator/cli.py:352` `cmd_spawn` |

`pre_tool_use.main` is the worst offender: task-turn resolution, scope flags, approval inheritance, warning, compaction marker, delta logging, feedback, baseline save, context injection, and the session nudge all in one flat function with five levels of `if`. The delta/feedback block (`pre_tool_use.py:161-195`) is copy-pasted almost verbatim into `stop_session.py:40-77` (a 35-line duplicate incl. the `prev_input > 0 or prev_cache > 0` fallback). `estimator.score_flags` is called three times on the same flags across PRE and Stop.

**Dead code (grepped repo-wide, excluding archived scribe backups):** `logger.save_snapshot/load_snapshot/delete_snapshot` (`logger.py:369-393`) — only `test_hooks.py:175-180` calls them; `count_entries` (`logger.py:162`) — no callers; `count_tasks` and `read_feedback` — tests only. `pre_tool_use.py:13` imports `json` unused; `:22` imports `read_payload` under a `noqa` comment that says `hook_allow` is used ("hook_allow used below") — the comment is about the wrong name.

**Naming/structure nits:** `logger.py` has no module docstring or shebang (the only file in the cluster without one); `report.py:35` uses `__import__("sys")` inline rather than importing `sys`; `report.py:458` re-imports `json as _json` inside `main` although `json` is already imported at `:14`; `report.py:70-76` imports `core.session` without the `sys.path` insert every other file uses, so it always hits the `ImportError` fallback when run as a script. `validate_findings.py` and `validate_consolidation.py` both re-implement the same `required string field` loop and the same `--check-files` block (`validate_findings.py:169-181` vs `validate_consolidation.py:118-129`). `_percentile` is duplicated in `report.py:359` and `tune.py:56`; `_read_jsonl`/`load_jsonl` duplicated in `report.py:27` and `tune.py:34` and again as `logger.read_log`.

**No TODO/FIXME/HACK markers** in non-test code. Comments are unusually good — many cite the ATK-NNN finding that motivated a guard (e.g. `validate_findings.py:32,98`), which is exactly the paper trail harden promises.

---

## 6. Tests

`poetry run pytest budgeter harden incubator -q` → **151 passed in 21.99s**.

- **Budgeter (`test_hooks.py`, 1,176 lines, ~40 tests).** Hermetic: `APIARY_BUDGETER_TEST_ISOLATION=1` makes the logger raise if any write targets production paths (`logger.py:26-48`), and hooks are run as real subprocesses against a temp project with `.claude/budgeter.json`. That isolation guard is a genuinely good pattern. Coverage is wide on estimator arithmetic and the nudge tiers but **shallow on the delta pipeline**: every hook-level test passes `transcript_path: ""`, so `tokens == 0` (`test_hooks.py:217-241`) and none of B2/B3/B5 can be caught; `test_cont_continuation_inherits_task_turn` (`:243-292`) does not run the hook at all — it re-implements the inheritance logic inline and asserts on its own copy, so B7 is untestable by construction. No test for a corrupt baseline (B1), a >64 KB PostToolUse payload (B6), a stale lock (B8), or a Stop-between-turns sequence.
- **Harden (82 tests across 5 files).** Subprocess-driven, hermetic via `HARDEN_TMP_DIR` for the counter. Good negative coverage of each validator (type guards, caps, coverage gaps, degrade). Nothing tests the skill's *control flow* — necessarily, since it is prose — and nothing exercises `check_path_escape` from a cwd that is not the repo.
- **Incubator (`test_cli.py`, 23 tests).** Mostly mocked (`_fetch_spec`, `_run_scribe`, `core_install.install`), plus one real end-to-end spawn against a throwaway fake apiary with real scribe subprocesses (`test_cli.py:322-398`) — the best test in the cluster. The verify contract is tested from both sides. No test for the argv-length failure (B9) or the `done`-fails-after-`add` path (B10).

---

## 7. Skills / prompts review

Token estimates use ~4 bytes/token.

| File | Bytes | ~Tokens | Assessment |
|---|---|---|---|
| `harden/commands/harden.md` | 37,225 | ~9,300 | **Very long but mostly earned.** Clear roles, explicit stop conditions (empty findings, all-deferred, budget), explicit retry ceilings ("once, then ask/degrade"), and every CLI invocation cross-checks against argparse (`validate_and_assign.py findings/response/consolidation` flags, `round_counter.py` verbs, `lenses.py list/codes/json`, `query_request.py --request-id --cwd`, `scribe/notes.py add --type --content --session-id --auto`, `get`). Problems: (a) `:533` describes a "parent-session fallback path" that does not exist; (b) `:116-137` has the model write and delete a `__harden_size_check.py` in the repo root — a stray file if the run aborts, for a job `python -c` could do; (c) `:228,430,649,655` mandate `AskUserQuestion`, which the user has said they dislike (memory: "Ask in plain text"); (d) the `[rid:…]` tag in the Agent `description` (`:177`) works but uses a 3-5-word field as a side channel; (e) the round-2+ Defender continuation depends on the Agent tool surfacing an agent ID — undocumented behaviour that the skill treats as stable. Runaway risk is low: every loop has a hard `--rounds` cap and a one-retry rule. An LLM will follow it, but 9k tokens of instructions on every `/harden` is the price. |
| `harden/agents/attacker_lens.md` | 2,742 | ~700 | Tight. Explicit lens, seam rules, read-only, JSON-only, "do NOT include id/category/lens" matches `sanitize(lens=…)`. Good. |
| `harden/agents/consolidator.md` | 3,375 | ~850 | Tight. Exactly-once accounting rule (`Rules 2`) mirrors the validator. Good. |
| `harden/agents/defender.md` | 3,383 | ~850 | Good; the repeated "you MUST use Edit" is justified by history (`544d5e6`). Says "ATK-NNN IDs" throughout although in multi-lens mode it receives `CON-NNN` — harmless because the validator is prefix-agnostic, but a reader could think the wrong path ran. |
| `harden/agents/attacker.md` | 2,390 | ~600 | Legacy path only; still consistent with `VALID_CATEGORIES` and `CATEGORY_MAP`. |
| `incubator/commands/incubator.md` | 5,233 | ~1,300 | Precise, matches argparse (`spawn --path --spec-note-id --session-id`, `verify --path`, `notes.py list --type context --last 1`). The "CLI will: 1-4" list at `:64-68` omits the install and hook-install steps that `cmd_spawn` actually performs; exit-code table matches `cli.py:19-26`. The "why Step 5 exists" paragraph is a good example of a skill teaching from an incident. Justified. |
| `budgeter/commands/budgeter-log.md` / `-warn.md` / `-session-warn.md` | 604 / 738 / 1,036 | ~150-260 | Cheap but **broken** (B4): they toggle `~/.claude/…` while `core/flags.py` reads `<repo>/.claude/apiary/flags/…`. Should shell out to `core/flags.py` (or a tiny CLI) rather than hand-rolling a bash toggle. |
| `budgeter/commands/budgeter-setup.md` | 1,325 | ~330 | Accurate (`poetry run apiary install --target` exists: `pyproject.toml:23`). Fine. |

---

## 8. Docs vs reality

| Doc | Says | Reality |
|---|---|---|
| `docs/architecture/hook-lifecycle.md:53-60` | baseline at `budgeter/tmp/baseline_<session_id>.json`, "cleaned up … on session end" | `<session_id>_baseline.json` (`core/session.py` `tmp_path`); cleaned at the end of **every turn** by the `Stop` hook. `last_verified: 2026-04-02`. |
| `hook-lifecycle.md:41-49` | `[CONT]` chains continuation turns | Dead in practice (B7). |
| `hook-lifecycle.md:64-73` | warnings gated by `min_tasks`, `scope_threshold`, `expensive_percentile` | Code keys are `warn_score_threshold`, `min_flagged_tasks`, `expensive_percentile_feedback` (`pre_tool_use.py:128`, `estimator.py:203`, `config.json:32-34`). |
| `README.md:365-374` and `docs/reference/config-files.md:114-127` | config fields `min_tasks`, `expensive_token_threshold`, `expensive_percentile`, `similarity_top_n`, `scope_rules`, `scope_weights`, `scope_threshold` | None of these exist in `config.json` or are read anywhere (grepped `budgeter core docs`). Actual keys: `scope_keywords`, `breadth_keywords`, `investigative_keywords`, `file_count_threshold`, `step_count_threshold`, `rule_weights`, `warn_score_threshold`, `min_flagged_tasks`, `expensive_percentile_feedback`, `session_warn_*`. |
| `config-files.md:131` | `.claude/budgeter.json` "loaded via `core/config.py` with `budgeter/config.json` as the defaults fallback" | `logger.load_config` (`logger.py:136-144`) reads one file, no merge; budgeter never imports `core.config`. |
| `docs/reference/slash-commands.md:41-43`, `file-storage.md:41-42` | flags at `~/.claude/budgeter-*-enabled` | `<repo>/.claude/apiary/flags/` since `2149090`. |
| `README.md:378-386` (Reporting) | omits `--by-agent`, `--by-request`, `--weighted`, `--feedback`, `--grouped` | `docs/reference/cli-tools.md:153-171` has them all; README is stale. |
| `incubator/templates/CLAUDE.md.tmpl:22` | `budgeter/report.py --since 7d` | `report.parse_date` is `strptime("%Y-%m-%d")` (`report.py:59-60`) → `ValueError`. Every spawned repo ships a crashing example. |
| `incubator/templates/gitignore.tmpl:21-27` | ignores `.apiary/scribe/…`, `.apiary/budgeter/…` | Spawned repos have no `.apiary/` dir since `55ae7ba`; state lives in main apiary's `.repos/`. Dead entries. |
| `harden.md:533` | "parent-session fallback path" aborts on overrun | Does not exist (`lib/query.py:20`). |
| `cli-tools.md:508-523` | `round_counter.py` "same interface as `refiner/round_counter.py`" | Also has `defender --set/--get` (`round_counter.py:65-78`) — documented at `:523` in the format line only. Minor. |
| `harden/agents/defender.md` | "ATK-NNN IDs" | Receives `CON-NNN` on the default multi-lens path. Cosmetic. |
| `README.md:393` "Testing: `python budgeter/test_hooks.py`" | | Still works (`test_hooks.py:760+` has a manual runner) but `code-style.md` says `unittest`/no pytest while the whole cluster is run via `poetry run pytest` and `test_hooks.py` uses pytest's `tmp_path` fixture (`72bf4fa`). The standard is out of date, not the tests. |

---

## 9. Verdicts

| Component | Verdict | Reason |
|---|---|---|
| `core/hook_context.hook_allow` (as used by budgeter) | **rewrite** | Emits a permission `allow` on every monitored call (B0). Should only add context. |
| `budgeter/hooks/pre_tool_use.py` + `stop_session.py` (logging core) | **improve** | The PRE-to-PRE idea is fine; the measurement has four concrete, fixable defects (B1, B2, B3, B5). Wrap `main`, dedupe by `message.id`, count cache creation, skip zero-delta PREs, make `save_baseline` atomic. |
| `budgeter` warning feature (`estimator.detect_scope_flags` / `estimate_magnitude` / `tune.py` / feedback plumbing) | **delete** | 9% precision after 5 months and 3,700 tasks; rules hover at the 25% base rate; the signal it needs (how many tools the task will take) is not observable at first-tool time. It has not earned its ~400 lines of estimator + 300 lines of tuner + feedback JSONL + baseline fields + three flags. Keep `session_length_nudge` (a different, trivially-correct feature) and the log. |
| `[CONT]` chaining + approval inheritance | **delete** | Dead due to per-turn baseline cleanup; the injected instruction costs context in every session for nothing. If task-level attribution matters, key on the transcript's `parentUuid`/turn structure instead of a baseline file. |
| `budgeter/hooks/post_tool_use.py` | **improve** | Raise or remove the 64 KB stdin cap (B6); it is the only source of subagent cost. |
| `budgeter/lib/logger.py` | **improve** | Drop snapshot/count dead code; add stale-lock expiry; atomic baseline write; module docstring. |
| `budgeter/report.py` | **keep** | Works; fold in `--cwd`, dedupe the JSONL reader. |
| `budgeter/log_agent_cost.py`, `query_request.py`, `lib/query.py` | **keep** | Small, tested, used by runner and `/harden`. |
| `budgeter/commands/budgeter-*.md` toggles | **rewrite** | Wrong path since May (B4); make them call `core/flags.py`. |
| `harden/` Python validators + `assign_ids` + `lenses` | **keep** | Real value: they are what makes an LLM loop deterministic and debuggable. Trim the duplicated field-check loops. |
| `harden/commands/harden.md` | **improve** | Sound design, one false claim (`:533`), a stray-file step, AskUserQuestion against user preference, and 9k tokens of load. Worth a pass to cut 30% without losing the contracts. |
| `harden/agents/*.md` | **keep** | Tight, contract-matched. |
| `harden/round_counter.py` | **keep** | Fine; make `reset` delete. |
| `incubator/cli.py` | **improve** | Sound sequencing and rollback; fix argv-length migration (B9) and the duplicating recovery hint (B10); stop reaching into `state._find_entry_by_path`. |
| `incubator/commands/incubator.md` | **keep** | Accurate and short. |
| `incubator/templates/*` | **improve** | Fix the crashing `--since 7d` example and dead `.apiary/` ignores. |
| `docs/architecture/hook-lifecycle.md`, budgeter config docs | **rewrite** | Describe a system (min_tasks, percentile, CONT, session-end cleanup) that does not exist as written. |

---

## 10. Top 10 recommended changes (ranked by value/effort)

1. **Stop voting on permissions: make `core/hook_context.hook_allow` omit `permissionDecision` unless a decision is intended**, then re-verify that unlisted Bash commands prompt again in a bootstrapped repo. Fixes B0. One line, repo-wide safety impact. — **S**
2. **Wrap `main()` in try/except in all three budgeter hooks and write baselines atomically** (`tempfile` + `os.replace`, as `tune.py:288-300` already does). Fixes B1, the only bug that visibly breaks a session. — **S**
3. **Dedupe transcript usage by `message.id` in `get_cumulative_tokens`.** Fixes the 2-3× over-count (B2) that feeds `/harden`'s budget. — **S**
4. **Skip logging when `tokens_now == baseline["tokens"]`** (no API call happened). Removes 25% phantom entries (B3). — **S**
5. **Count `cache_creation_input_tokens`** in cumulative, last-call and nudge figures, and add it as a fourth split field. Fixes B5 and makes `--weighted` honest. — **S**
6. **Point the three budgeter toggle skills at `core/flags.py`** (e.g. `python launch.py core/flags.py toggle budgeter-warn`, adding a 10-line `__main__`). Fixes B4 and the two stale doc tables. — **S**
7. **Remove the warning subsystem** (estimator rules, `estimate_magnitude`, `tune.py`, feedback JSONL, `predicted_cost`/`warning_fired`/`scope_flags` baseline fields, `budgeter-warn` flag) and the `[CONT]` instruction; keep `session_length_nudge`. Deletes ~800 lines and one confusing flag, and `pre_tool_use.main` shrinks by half. — **M**
8. **Raise `post_tool_use.py`'s stdin cap to read everything** (payloads are bounded by Claude Code, not by us) or at least to a few MB, and log a stderr line when JSON fails. Fixes B6. — **S**
9. **Use `--content-file` in `incubator/_migrate_spec`**, catch `OSError` around the subprocess, and change the recovery hint to "close the original" when `add` already succeeded. Fixes B9/B10. — **S**
10. **Add stale-lock expiry to `_file_lock`** and extract the duplicated delta-logging block from `pre_tool_use.py:161-195` / `stop_session.py:40-77` into one `logger` function; in the same pass rewrite `hook-lifecycle.md` and the budgeter config tables to match the code, fix `harden.md:533`, replace the `__harden_size_check.py` dance with `python -c`, and fix `CLAUDE.md.tmpl:22` / `gitignore.tmpl:21-27`. Fixes B8 and the doc drift. — **M**

One further item worth a ticket: give `/harden`'s worktree a cleanup step on Approve (or at least delete the branch on Discard).

---

## Addendum — Claude Code hook semantics (confirmed against the docs)

A `claude-code-guide` agent checked the Claude Code documentation; results, with the doc it cited:

1. **`Stop` fires once per turn** ("When Claude finishes responding"); `SessionEnd` is the once-per-session event — hooks reference, lifecycle table. This confirms B7: `stop_session.py` deletes the baseline after every response.
2. **`permissionDecision: "allow"` bypasses the interactive permission prompt.** Across multiple hooks "the most restrictive answer applies, in the order `deny`, `defer`, `ask`, `allow`"; a hook `allow` cannot override settings deny rules or connector/MCP `ask` settings — hooks guide, "Hooks and permission modes". This confirms B0.
3. **Non-zero exit other than 2 → non-blocking error, the action proceeds**; default `command` hook timeout is **10 minutes** — hooks guide. This confirms B1's blast radius (noisy, not blocking) and sharpens B8 (a hung lock wait is bounded only by that timeout).
4. **`tool_response` fields for `Agent` are not documented** ("internal to Claude Code and changes between versions"); subagent transcripts live in separate files (`<session-id>/subagents/agent-<agentId>.jsonl`) — sessions doc. `post_tool_use.py`'s reliance on `tool_response.totalTokens` is therefore an undocumented contract; it evidently works today (1,560 Agent entries in the log) but should be guarded with a stderr warning when the key is absent rather than a silent `exit(0)` (`post_tool_use.py:58-61`).
5. **One API turn is written as several consecutive assistant JSONL lines sharing `message.id` and identical `usage`** — dedupe by `message.id` to count correctly (confirmed by the docs agent via a widely-cited format write-up, and empirically above: 228/228 multi-line messages identical). Thinking tokens are a sub-field of `output_tokens`; total input = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`, billed at 1×, 1.25-2×, 0.1× — prompt-caching doc. This confirms B2 and B5.
