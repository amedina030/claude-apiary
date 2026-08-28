---
type: architecture
title: "Core subsystem review"
scope: project
description: Deep review of core/: install/registry/drift/mailbox/doctor, hooks, shared utilities (2026-08-26 deep review, subsystem appendix)
framework_version: "1.0"
last_verified: 2026-08-26
---

> **Snapshot of 2026-08-26; superseded by the remediation — see CHANGELOG. Deleted at close-out (T-2026-271).**

# Code review: `core/` (claude-apiary)

Read-only review, 2026-08-26. Every non-test `.py` under `core/` was read in full (39 files, 6,746 lines); the 25 test files (3,822 lines) were skimmed for coverage quality. Design reference: `docs/architecture/per-repo-install.md`, `docs/standards/code-style.md`. Test run: `poetry run pytest core -q` → **309 passed in 74.5s**.

---

## 1. What it is

`core/` is the shared substrate for the toolkit. It has three layers:

- **Install / registry layer** (the "per-repo model", 2026-05): `core/cli.py` is the `apiary` console script (`pyproject.toml:23`) with verbs `install`, `uninstall`, `self-bootstrap`, `doctor [check]`, `mailbox [--list]`, `cascade-fix`, `version` (`core/cli.py:136-189`). `core/install.py` writes `<repo>/.claude/{settings.json,commands/,apiary/{launch.py,main-apiary-pointer.json,self-pointer.json,version.json,flags/,session-tmp/}}`, the CLAUDE.md managed zone, a `.gitignore` block, and a git `pre-commit` secret-scan hook; `core/utils/state.py` owns the registry at `<main-apiary>/.repos/registry.json` + `next_id`, the FileLock discipline, and the pin-file helpers. `core/drift.py` / `core/mailbox.py` / `core/cascade.py` / `core/doctor.py` handle "the repo moved" scenarios.
- **Hook layer** (`core/hooks/`): 13 scripts wired by `core/hooks_factory.py` into every bootstrapped repo's `settings.json` through the generated launcher (`core/launcher_template.py`). Per Bash tool call in this repo: **9 PreToolUse** processes (`per_repo_drift_check`, `check_install`, `inject_session`, `startup_hook`, `learnings_inject_hook`, `pre_push_doc_conformer`, `pre_push_secret_scan`, budgeter, docs reminder), 2 PostToolUse, 3 Stop, plus `startup_prompt_hook` on UserPromptSubmit (verified from `.claude/settings.json`).
- **Shared utilities**: `flags.py` (per-repo toggle files), `hook_context.py` (hook JSON responses), `hooks_lib.py` (settings.json hook merge/remove, interpreter resolution), `session.py` (`SessionId`, identity files), `startup.py` (session init + summary), `sanitizer.py`, `secret_patterns.py`, `context_rules.py`, `apiary_profiles.py` + `utils/jsonc.py`, `git_hooks.py`, `utils/filelock.py`, `utils/project.py`, `targets.py`, `transcript.py`.

---

## 2. Architecture assessment

**What is sound.** The CLI → single-purpose-module split (`cli.py:4-13`) is right and makes the install path genuinely testable (`test_install.py` runs real `git init` targets against a fake main-apiary). Registry mutations consistently go through `.tmp` + `os.replace` (`state.py:125-132`) under `FileLock(registry_path)` (`install.py:172`, `drift.py:138`, `mailbox.py:130`, `cascade.py:50`, `uninstall.py:83`). The gate hooks separate a pure, unit-tested core from an I/O shell (`pre_push_doc_conformer.command_pushes` `:79-88`, `pre_push_secret_scan.scan_diff` `:151-159`, `learnings_inject_hook.score_learnings` `:102-154`, `research_capture_reminder.should_remind` `:64-69`). `secret_patterns.py` correctly centralises the literal-credential table shared by both gates. `apiary_profiles.py` + `jsonc.py` are small, well-specified, and thoroughly tested.

**Two generations coexist and both run.** The pre-migration "global `~/.claude`" generation is still live on every tool call:
- `core/session.py:11,80-84` (`~/.claude/.session-identity-*.json`, `~/.claude/tmp/<uuid>_*` flag files), `core/startup.py:28,95` (writes identity + reads `~/.claude/.session-history.json`), `core/hooks/save_transcript.py:22-24,63,89` (writes `~/.claude/.session-history.json` and `.last-session.json`), `core/hooks/check_install.py:24-25` (reads a manifest nothing writes any more), `core/hooks/startup_hook.py:20` (checks `~/.claude/CLAUDE.md` for a managed zone that per-repo installs never put there — `install.py:328` writes `<repo>/CLAUDE.md`), `hooks_lib.hook_cmd`'s `$HOME/.claude/apiary_launch.py` mode (`hooks_lib.py:109-133`, no non-test caller), `scripts/uninstall_hooks.py:37` and `scripts/install_context_rules.py:44` (both default to `~/.claude`).
- The per-repo generation (`state.py` pin helpers, `install.py`, `drift.py`, …) sits on top and is documented as the only model. The doc promises `sessions/history.json` and `identity-<short>.json` under `.repos/<slug>/sessions/` (`per-repo-install.md:92-95`); those files exist on disk dated 2026-05-05 (one-time phase-3 migration; only `.pyc` remnants of those scripts survive under `scripts/__pycache__/`) and **no live code writes them**.

**Two target-identity models coexist.** `state.resolve_target_state_dir` (`state.py:253-324`) lazily registers a repo by *path* and writes a legacy breadcrumb `<repo>/.apiary/pointer` (`:319`), which `find_state_dir` (`:338-360`) then depends on. `apiary install` (`install.py:57-144`) registers by *uid* with `.claude/apiary/self-pointer.json` and never writes `.apiary/pointer`. So a repo bootstrapped only via `apiary install` has no breadcrumb → `find_state_dir` returns `None` → `startup_prompt_hook.py:248` silently skips compass injection and `gui/scribe_aggregator.py:106` / `scribe/api.py:153` cannot locate it. `resolve_target_state_dir`'s only live caller is `scripts/bootstrap.py:219` plus the `python core/utils/state.py` entry point (`state.py:485-494`), so it exists mainly to keep the legacy breadcrumb alive.

**Duplication inside core/.**
- git-root resolution ×4: `state._git_repo_root` (`state.py:69-90`), `flags._per_repo_root` (`flags.py:30-48`), `git_hooks.current_repo` (`git_hooks.py:38-52`), inline in `pre_push_doc_conformer.py:133-143` (and 4 more copies outside core: `scribe/notes.py:119`, `captures/store.py:51`, `compass/store.py:51`, `researcher/store.py:43` — `state.py:72-74` even says "mirrors the helper that already exists in scribe/captures/researcher").
- JSON-object readers ×5 with identical semantics: `state._load_registry` (`:112-122`), `state._read_pointer` (`:183-194`), `state._read_json_file` (`:406-414`), `mailbox.read_message` (`:95-106`), `hooks_lib.load_settings` (`:142-150`), `config.load_config`, launcher `_read_json`.
- Atomic tmp+replace writers ×7: `state._save_registry` (`:125-132`), `state.allocate_next_id` (`:161-163`), `state._write_pointer` (`:177-179`), `state._write_json_file` (`:417-423`), `install._write_launcher` (`:208-210`), `install._write_bootstrap_state` (`:463-465`), `mailbox.write_message` (`:81-83`). `hooks_lib.save_settings` (`:153-157`) and `config.write_config` are *not* atomic, so the pattern is also inconsistent.
- `_now_iso` in `state.py:65` and `targets.py:41` plus inline in `save_transcript.py:53`.
- The "main-apiary is uid 1" constant is `drift._MAIN_APIARY_UID` (`drift.py:26`), a literal `1` in `cascade.py:57`, and a literal `1` in `install.py:100`.
- The `APIARY_RUNNER_SUBPROCESS` guard is copy-pasted into 9 hooks; the once-per-session flag dance is copy-pasted into 5 (`check_install.py:84-92`, `inject_session.py:42-48`, `startup_hook.py:52-57`, `startup_prompt_hook.py:123-128`, `research_capture_reminder.py:118-125`).
- Report printing for mailbox and cascade is duplicated between `cli.py:108-114`/`doctor.py:323-333` and `cli.py:117-127`/`doctor.py:336-347` — four user-facing entry points for two actions.
- Registration entry construction is duplicated between `state.py:308-316` and `install.py:188-196`.

**Is drift / mailbox / cascade justified for a single-user, single-machine toolkit?** Partly. The problem (repo moved → `.repos/<slug>` mapping stale) is real. But the machinery is ~900 lines (`drift.py` 246, `mailbox.py` 179, `cascade.py` 77, `doctor.py` 395, pin helpers in `state.py` ~100) and its central promises are not wired:
- The mailbox's raison d'être ("the drift hook never holds the registry lock", `mailbox.py:6-8`) is already violated: `drift._handle_drift` takes `FileLock(registry_path)` (`drift.py:138`) and calls `allocate_next_id` (`:145`), which writes `next_id` inside main-apiary. Since the hook already holds the registry lock at that moment, it could simply update `registry.json` in place; instead it writes a message file that nothing drains automatically (see Bug 2). The mailbox therefore adds a second consistency domain, a manual step, and a failure mode, for no isolation benefit.
- `version.json` is written (`install.py:111-114`) and **never read** by any live code (`state.read_version` has zero callers; only `migrations/v0_0_0_to_v0_1_0.py:23` mentions the file). There is no migration runner in `core/` (`self_bootstrap.py:32` only uses `migrations` as an existence sentinel) and no `apiary update` verb, despite docs.
- Copy detection (`drift._classify_as_copy`, `:229-245`) only ever fires for `cp -r`/backup-restore copies, because `.claude/` is gitignored so clones never carry a self-pointer. Reasonable, but the classification logic + a new uid + a `register_copy` message is a lot for that case.
- `doctor` has 8 checks but none verify the thing the pin model exists for: that each registered repo's `self-pointer.uid` equals its registry key and its `main-apiary-pointer` points at this checkout. `check_pointers` (`doctor.py:51-76`) only inspects main-apiary's own pointer.

Verdict: keep the pin files + registry + cascade (cheap, correct), delete the mailbox and apply drift updates inline under the lock already held, and either wire `version.json` to something or stop writing it.

**Hook fan-out is expensive and partly pointless.** Of the 9 PreToolUse processes per Bash call, `check_install.py` can never do anything useful (Bug 13), `startup_hook.py` always yields nothing in the per-repo model (Bug 14), `learnings_inject_hook` is registered three times (`hooks_factory.py:90-92`) and short-circuits on a flag that is off in this repo (`.claude/apiary/flags/` has no `learnings-inject-enabled`), and `check_install_stop.py` is an explicit no-op Stop hook (`check_install_stop.py:17-18`) still registered at `hooks_factory.py:116`. Each is a fresh interpreter + launcher subprocess (the launcher itself is a second Python process per hook: `launcher_template.py:110-113`), so that is ~18 interpreter starts per Bash call, roughly half of them no-ops.

---

## 3. Bugs and correctness risks (ordered by severity)

**Bug 1 — `apiary doctor <check> --fix` is unreachable through the console script.** `cli.py:162-170` defines the `doctor` subparser with only `subcommand` and `--apiary-repo`; `_cmd_doctor` (`cli.py:84-91`) forwards only those. Verified: `poetry run apiary doctor --fix` → `apiary: error: unrecognized arguments: --fix`, exit 2. Yet `doctor.py:74`, `doctor.py:122`, `SETUP.md:336` and `docs/reference/cli-tools.md:943` all instruct the user to run `apiary doctor pointers --fix` / `mailbox --fix`. `test_doctor.py:235-268` tests `--fix` only via `doctor.main([...])`, and `core/cli.py` has no tests at all, so nothing caught it.

**Bug 2 — The mailbox is never drained automatically.** `per-repo-install.md:206-208` and `SETUP.md:281` say main-apiary processes it "on its own session open". The only callers of `mailbox.process_pending` are `cli.py:108` and `doctor.py:326` (grep across `core/ scripts/ docs/ gui/ scribe/`). `drift.check_and_handle` / `_handle_main_apiary_drift` (`drift.py:51-100,190-226`) never call it. Consequence chain after `mv repoA repoB`: the moved repo's self-pointer is fixed (`drift.py:172-174`) but `registry.json` keeps the old path until someone runs `apiary mailbox`; meanwhile `doctor registry`/`unreachable` flag it (`doctor.py:104-108,275-279`), `check_stale` skips it (`:186-187`), and if main-apiary itself moves before the drain, `cascade_fix` skips the entry as "path missing" (`cascade.py:64-66`) → the moved repo's `main-apiary-pointer.json` is never rewritten → `_verify_main_apiary` fails (`drift.py:106-108`) → that repo runs vanilla forever and cannot even queue a new message because it can no longer find main-apiary.

**Bug 3 — `process_pending` can delete messages without applying them.** `read_message` validates only `kind` (`mailbox.py:104`). `msg["new_path"]` (`:147`), `msg["name"]` / `msg["version"]` (`:158-161`) raise `KeyError` mid-loop. Failing input: mailbox contains `5.json` (a valid `update_path`) and `9.json` = `{"kind":"register_copy","from_uid":9}`. Outcome: `5.json` is already unlinked (`:172`), the `KeyError` escapes the loop, `_save_registry` (`:177`) never runs → the update for uid 5 is silently lost and the lock releases via exception. Additionally `register_copy` overwrites an existing registry entry unconditionally (`:157-165`), so a stale/replayed message whose `from_uid` collides with a live repo clobbers that repo's entry.

**Bug 4 — Re-install after registry loss leaves `self-pointer.uid` ≠ registry uid, with no detection.** `install.py:103-110` keeps an existing self-pointer untouched, while `_register_or_update` (`:162-199`) matches only by path and allocates a fresh uid when the entry is missing. Failing state: registry entry removed (a partially failed `uninstall` — see Bug 6 — or a fresh clone of main-apiary on the same machine, since `.repos/` is gitignored) then `apiary install --target repo`. Outcome: registry says uid 17, self-pointer still says uid 7, state dir `<name>-17` is created, but the launcher builds `<name>-7` from the self-pointer (`launcher_template.py:68-73`), finds no such dir, and does not set `APIARY_TARGET_STATE_DIR` → `scribe_state_dir` falls back to `<repo>/.apiary/scribe/` (`scribe/notes.py:170-176`). The drift hook also sees `entry = registry.get("7") is None` and classifies every session as a "move" of an unknown uid (`drift.py:140-142,169-187`), queueing an `update_path` message that `process_pending` will reject as "unknown uid" (`mailbox.py:141-146`) on every drain. No doctor check compares pin files to the registry.

**Bug 5 — uid-1-means-main-apiary is a convention, not an invariant, and the failure mode is destructive.** `drift.py:75` dispatches to `_handle_main_apiary_drift` purely on `uid == 1`; `cascade.py:57` and `install.py:100` hardcode the same. But `state.resolve_target_state_dir` (`state.py:302`) lazily allocates the next uid to *any* repo, so on a fresh machine where `scripts/bootstrap.py` (or the `state.py` `__main__`) runs in some other repo before `apiary self-bootstrap`, that repo becomes uid 1. If that repo is later moved, `_handle_main_apiary_drift` (`drift.py:190-226`) rewrites its `main-apiary-pointer` to point at *itself* (`:207-211`) and then `cascade_fix(repo_root)` reads `<that-repo>/.repos/registry.json` — normally absent, so harmless; but if `.repos/` existed there it would rewrite every registered repo's `main-apiary-pointer.json` to a non-apiary directory (`cascade.py:71-73`). On this machine uid 1 is `claude-apiary`, so it is currently correct; nothing enforces it and doctor does not check it.

**Bug 6 — `uninstall` mutates the registry first and is not transactional.** `uninstall.py:83-101` deletes the registry entry before `shutil.rmtree(pin)` (`:107`), `remove_hooks` (`:115`), and the CLAUDE.md edit (`:119`). Any exception in those (e.g. a `PermissionError` on Windows while a hook process still has `launch.py` open) leaves pin files + hooks in place with no registry entry → next session is exactly Bug 4. There is also no guard against `apiary uninstall --target <main-apiary>` — it would `rmtree` main-apiary's own `.claude/apiary/` and delete registry entry 1 (and with `--remove-data`, delete `.repos/claude-apiary-1/` including apiary's own scribe notes), whereas `git_hooks.install` does guard the analogous case (`git_hooks.py:144-148`).

**Bug 7 — `_apply_profile_permissions` clobbers user-owned top-level keys on every re-install.** `install.py:277-283` writes every non-`hooks` key of the resolved profile verbatim over `<repo>/.claude/settings.json`. The comment at `install.py:31-33` ("Anything outside this set is preserved as-is on re-run") describes `_APIARY_OWNED_KEYS`, which is defined and **never referenced** anywhere in the repo. Failing input: user adds `"permissions": {"allow": ["Bash(make *)"]}` to a bootstrapped repo's `settings.json`; `apiary install`/`self-bootstrap` replaces it with the profile's list. Same for any future profile key.

**Bug 8 — `is_apiary_entry` deletes user hooks by directory-name coincidence.** `hooks_lib.py:48-56` treats any hook whose JSON contains `/scribe/`, `/runner/`, `/harden/`, `/refiner/`, `/docs/hooks/` as apiary-owned; `register_hooks` drops such entries on every install (`:176-181`) and `remove_hooks` deletes them on uninstall (`:239-242`). Failing input: a user hook `{"type":"command","command":"python scripts/runner/lint.py"}` in `<repo>/.claude/settings.json` → silently removed on the next `apiary install`. No test covers a non-apiary entry that happens to contain one of the substrings.

**Bug 9 — The drift check is not once-per-session; it rewrites `self-pointer.json` on every tool call.** `per_repo_drift_check.py:2` says "on first tool call of a session" and the design doc says "on every session open", but unlike the other core hooks there is no `flag_path` guard; `check_and_handle` writes the self-pointer (`drift.py:96-97` / `:197-198`) on the no-drift path every time. Every hook dispatch also reads that same file through the launcher (`launcher_template.py:65`), and Claude Code runs the 9 PreToolUse hooks concurrently. On Windows, `tmp.replace(p)` (`state.py:422`) raises `PermissionError` if any other process has `p` open (Python opens without `FILE_SHARE_DELETE`); the hook's blanket `except Exception: pass` (`per_repo_drift_check.py:47-50`) hides it and leaves `self-pointer.json.tmp` litter. Not data-corrupting, but a write-per-tool-call plus a silently-swallowed race, on a file whose semantics are "session open".

**Bug 10 — `install()` is not transactional and raises raw exceptions past the CLI.** `_write_claude_md_zone` calls `cr.find_managed_zone` (`install.py:330`) which raises `ZoneTamperError` (`context_rules.py:266-275`) on a duplicated/missing sentinel — after the registry entry, pin files, and settings.json have already been written (`:84-117`). `_write_bootstrap_state` does an unguarded `json.loads` (`:452`) on an existing file. Neither is wrapped in `InstallError`, so the user sees a traceback and half an install.

**Bug 11 — `save_transcript.py` violates the "hooks must not crash" rule and runs every turn.** `main()` (`save_transcript.py:66-92`) has no try/except; `FileLock` raises `TimeoutError` after 5 s (`filelock.py:33-35`), and any `OSError` on `~/.claude` propagates → non-zero Stop hook → Claude Code surfaces a hook error. Stop fires at the end of every assistant turn (as `check_install_stop.py:5-7` itself documents), so the history file is rewritten every turn.

**Bug 12 — Both push gates resolve the wrong repo for `git -C <path> push`.** `_segment_pushes` deliberately skips `-C <path>` (`pre_push_doc_conformer.py:40-42,69-71`) to still recognise the push, but both `_run` bodies then resolve the git root from the payload `cwd` (`pre_push_doc_conformer.py:131-143`, `pre_push_secret_scan.py:199-204`). `git -C ../other push` from apiary runs apiary's doc-conformer and scans apiary's outgoing commits → wrong block or wrong pass.

**Bug 13 — `check_install.py` is dead weight that gives wrong advice.** `MANIFEST_PATH` (`check_install.py:25`) is never written by anything (CHANGELOG line 72 records its removal; not present on this machine). If a stale manifest ever existed the hook would tell the user to run `python setup.py --global` (`:101`), which is a redirect stub that exits 1 (`setup.py`). Its only actual effect is writing a `~/.claude/tmp/<uuid>_install_checked` file per session (`:85-92`).

**Bug 14 — `startup_hook.py` inspects the wrong CLAUDE.md.** It reads `~/.claude/CLAUDE.md` (`startup_hook.py:20,82-84`); per-repo installs write the managed zone into `<repo>/CLAUDE.md` (`install.py:328`). The user's `~/.claude/CLAUDE.md` contains no zone, so `_context_rules_drift_line` always returns `""` — the hook never reports anything, and its remediation text points at `scripts/install_context_rules.py --check` (`:99`) which targets `~/.claude/CLAUDE.md` (`scripts/install_context_rules.py:44`).

**Bug 15 — `context_rule_error_reminder` false positives.** String-form responses trigger on the substring `"error"` (`context_rule_error_reminder.py:76`), so `grep -rn error src/` or any output mentioning "error" fires the behavioural reminder after a *successful* call.

**Bug 16 — `resolve_apiary_repo` prefers the source tree it is running from over everything except the env var.** `state.py:233-235` returns `_REPO_ROOT` whenever `core/install.py` and `VERSION` exist there, which is always true when the code runs from any apiary checkout. With a git worktree (this repo's branch-per-change workflow, and the `isolation: "worktree"` agent option), `poetry run apiary self-bootstrap` from the worktree targets the worktree's empty, gitignored `.repos/`, silently creating a second registry.

**Bug 17 — Minor.** `flags._per_repo_root` documents `APIARY_TARGET_REPO` as an override for tests/CLI (`flags.py:9-10`) but checks `CLAUDE_PROJECT_DIR` first (`:33`), so in a hook environment the override cannot win. `startup.run_init` ignores its `repo_dir` argument (`startup.py:81-97`). `learnings_inject_hook._run` (`:214`) and `startup_prompt_hook._run` (`:132`) fall back to *main-apiary's* root when the payload lacks `cwd`, so a cwd-less payload injects apiary's own learnings/state into another repo's session. `drift.py:75` `int(self_p.get("uid", -1))` raises `ValueError` on a non-numeric uid, swallowed by the hook, so a corrupted pin silently disables drift detection for that repo forever with no doctor visibility.

---

## 4. Security / safety

**S1 — Apiary writes to `~/.claude` on every session, contrary to the documented contract.** `per-repo-install.md:33-36`, `SETUP.md:306` and `PORTABILITY.md:10` state "apiary writes nothing here". Live writers: `startup.py:95` (`~/.claude/.session-identity-<sid>.json` via `session.py:80-81`), `save_transcript.py:63,89` (`.session-history.json`, `.last-session.json`), and the once-per-session flag files at `~/.claude/tmp/<uuid>_<suffix>` from `session.py:83-84` used by `check_install.py:85`, `inject_session.py:42`, `startup_hook.py:52`, `startup_prompt_hook.py:123`, `research_capture_reminder.py:118`. Nothing prunes them (`check_install_stop.py:10-12` explicitly punts). Measured on this machine: **3,389 files in `~/.claude/tmp`** and **779 identity files**, growing ~5 files per session, unbounded. `PORTABILITY.md:75` attributes the identity files to Claude Code; they are apiary's.

**S2 — `pre_push_doc_conformer` executes repo-provided code on push.** `pre_push_doc_conformer.py:146-155` runs `<pushed-repo>/docs/check_cli_claims.py` with the user's interpreter whenever a `git push` is issued from any directory in the session. Any repo the user pushes from (including a third-party clone they `cd` into inside a bootstrapped session) can plant that file and get code execution at push time. The guard "only acts if the repo ships the conformer" is exactly the attacker's condition.

**S3 — Secret-scan coverage gaps** (`core/hooks/pre_push_secret_scan.py`, `core/secret_patterns.py`):
- *Cumulative-diff blind spot.* The gate diffs `parent(oldest-outgoing)..HEAD` (`:221-227`). A secret added in commit A and removed in commit B — both outgoing — is absent from that diff but present in the pushed history. This is the most common real-world leak shape ("oops, committed it, then deleted it").
- *Scans `HEAD`, not the pushed ref.* `rev-list HEAD --not --remotes` (`:209`) ignores what `git push origin feature` actually pushes when HEAD is elsewhere, and `--remotes` excludes commits that exist on *any* remote (a fork counts).
- *Entropy gate is mathematically unreachable for short and hex values.* `_ASSIGNMENT` requires ≥12 chars (`:77`) and `_ENTROPY_MIN = 4.0` bits/char (`:79`). Max Shannon entropy of a 12-char string is log₂12 = 3.58, of a 15-char string 3.91 — so **no value under 16 characters can ever trigger**. Hex strings have at most 16 symbols (max 4.0 exactly); sampled random hex at lengths 32/40/64 averages 3.61/3.70/3.82 bits with **0 of 2,000 samples ≥ 4.0**. Every hex-encoded credential (HMAC keys, many vendor API secrets, `sha1`-style tokens) is invisible to the `high-entropy-assignment` rule. Random 20-char alphanumerics trip it only 67 % of the time.
- *Missing literal patterns* in `secret_patterns.PATTERNS` (`:45-91`): Stripe `sk_live_`/`rk_live_`, GitLab `glpat-`, npm `npm_`, SendGrid `SG.`, JWTs (`eyJ…`), Slack webhook URLs, Twilio `SK…`, GCP service-account JSON (`"private_key_id"`), Azure connection strings. `openai-key` (`:63`) also fires on any `sk-` + 20 chars, and `basic-auth-url` (`:88`) on every `user:pass@` right after the scheme example in docs.
- *Fail-open on timeout.* `git diff` has a 60 s budget (`:227`); on a large first push (base = empty tree, `:226`) it can time out → silent allow.

**S4 — Silent deletion of user hook config** (Bug 8) and silent overwrite of user `settings.json` keys (Bug 7) are safety issues in their own right: both happen without output.

**S5 — `cascade_fix` is the one place apiary writes into other repos** (`cascade.py:71-73`, as documented at `per-repo-install.md:222-224`). Combined with Bug 5 it can write a wrong path into every bootstrapped repo in one pass. It should at minimum refuse when `new_main_apiary_path` lacks `core/install.py` + `VERSION`.

**S6 — Launcher trust.** `launch.py` runs `<main_apiary>/<argv[1]>` with no path validation (`launcher_template.py:100-113`), and silently exits 0 on a missing script (`:101-103`) — a typo in `hooks_factory` disables a gate with no signal. The trust anchor is that `.claude/` is gitignored; the stepwise `.gitignore` block (`install.py:354-362`) keeps `settings.json` ignored, but a repo that tracked `.claude/settings.json` *before* bootstrap keeps tracking it (gitignore does not untrack), so a clone could carry hook commands. That is a Claude Code property, not apiary's, but apiary's install does not warn about it.

**S7 — Context injection sources are local-only** (learning bodies, `compass/personality.md`, `core/commands/apiary-context.md`, `docs/reference/cli-index.md`), so prompt-injection exposure is limited to the user's own notes. The sanitizer (`sanitizer.py`) is a mitigation for API refusals, not for injection, and it corrupts legitimate text: any `../` in a learning becomes `<scrubbed:path-traversal-unix>` (`:30`), `rm -rf` in a how-to becomes `<scrubbed:destructive-shell-rm>` (`:37`). The premise (specific token sequences cause API refusals) is asserted in the docstring (`:4-8`) but not evidenced anywhere in the repo.

---

## 5. Code quality

**Dead or orphaned code (callers verified by repo-wide grep, excluding `.venv`, archived notes, and the file itself):**
- `core/config.py` — `load_config`/`write_config` have **zero callers**; `docs/standards/code-style.md` still tells contributors to use it.
- `core/transcript.py` — `read_first_message` has zero callers (referenced only in 2026-04 archived handoffs).
- `core/hooks/extract_transcript.py` — a CLI, not a hook, with zero live references in any `*/commands/*.md`, `docs/`, or `.py`.
- `core/hooks/check_install.py` + `check_install_stop.py` — see Bug 13; the Stop hook is a literal no-op still registered (`hooks_factory.py:116`).
- `core/launcher_template.render()` (`:121-124`) — zero callers; `install.py:209` uses the constant directly.
- `install._APIARY_OWNED_KEYS` (`install.py:33`) — defined, never used (Bug 7).
- `state.state_dir_from_env` (`state.py:327-335`) — only its own tests call it.
- `state.read_version` / `version.json` — written, never read.
- `hooks_lib.hook_cmd` global-launcher branch (`hooks_lib.py:109-133`) — only `test_hooks_lib.py:52` exercises it.
- `git_hooks._classify` alias (`git_hooks.py:114-116`) — kept only so `scripts/install_git_hooks.py:40,52` can re-export it.
- `core/targets.py` — reachable only through the launcher (`cli-tools.md:961-967`); `verify` writes `verified_ok`/`last_verified` (`:87-88`) that nothing except its own `list` reads; wholly overlapped by `doctor registry`/`unreachable`.
- `startup.py` `init`/`summary` CLI subcommands (`:251-275`) — the hook imports `run_init`/`run_summary` directly (`startup_prompt_hook.py:30`); the argparse surface has no other caller but is documented in `cli-tools.md:98-116`.

**Stale narrative in comments/docstrings.** `MIGRATION-PLAN.md` does not exist but is cited as the spec in `cli.py:19`, `state.py:47,51,383`, `install.py:9`, `uninstall.py:3`, `self_bootstrap.py:3`, `drift.py:8`, `mailbox.py:3`, `cascade.py:4`, `doctor.py:8`, `hooks_factory.py:6`, `launcher_template.py:7`, `flags.py:15`, `pyproject.toml:22`, `SETUP.md:16`. `doctor.py:3-6,66,113,122` still say "phase-0 scaffold / `--fix` reserved / once phase 1 lands" though `--fix` exists (`:350-353`). `doctor.py:24-26` and `state.py:489` give `python ~/.claude/apiary_launch.py …` usage (retired launcher). `state.py:1-17` describes the pre-pin `.apiary/pointer` model as current. `hooks_lib.py:4-5` says "Used by setup.py". `context_rules.py:4,18,94` say the zone lives in `~/.claude/CLAUDE.md`. `install.py:269-272` references `core/apiary_bootstrap.py`, which does not exist.

**Conventions.** Mixed typing styles inside one file (`state.py:28` imports `Optional` and uses it at `:327,338` while using `X | None` everywhere else; `hooks_lib.py` uses `Dict`/`List` and `python_exe: Path = None`). Import placement is inconsistent with `code-style.md` §"File structure": half the hooks import at top level, half defer everything into `_run()` (`learnings_inject_hook.py:167-178`, `pre_push_doc_conformer.py:105-111`, `research_capture_reminder.py:85-92`); `pre_push_secret_scan.py:37-43` contains a comment explaining a real crash that the deferred style caused. `apiary_profiles.py:115-118` has a `try/except JsoncParseError: raise` no-op. `uninstall._remove_apiary_commands` recomputes main-apiary from `state_dir.parent.parent` (`uninstall.py:163`) although the caller has `apiary` in hand.

**Function length / nesting.** `startup_prompt_hook._run` is 165 lines with six numbered sections and seven `try/except Exception: pass` blocks (`:100-265`); `startup.run_summary` is 100 lines mixing state resolution, filtering, formatting, and a subprocess call (`:139-239`); `mailbox.process_pending` nests four levels (`:130-177`); `learnings_inject_hook._run` is 90 lines. `hook_context.hook_block` emits `permissionDecision: "block"` (`hook_context.py:51`) while Claude Code documents `allow|deny|ask` for PreToolUse (`"block"` is the legacy top-level `decision` value) — it evidently works today (the user reports the gate blocking), but should be normalised; `hook_allow` emits `permissionDecision` for `UserPromptSubmit`/`PostToolUse` events where it is not a defined field (`startup_prompt_hook.py:265`), whereas `context_rule_error_reminder.py:109-114` builds its own dict without it — two hooks in the same package disagree about the response schema, and `research_capture_reminder.py:14-16` asserts PostToolUse `additionalContext` "may silently no-op" while `context_rule_error_reminder` relies on it.

**Good hygiene, for the record:** no `TODO/FIXME/HACK/XXX` anywhere in `core/`; `encoding="utf-8"` is passed consistently; subprocesses are list-form with timeouts; `pathlib` throughout.

---

## 6. Tests

`poetry run pytest core -q` → **309 passed, 0 failed, 74.5 s**. The runtime is dominated by subprocess-per-test hook tests and real `git init` in `test_install.py`/`test_uninstall.py`.

**Well covered (behavioural, hermetic):** `test_install.py` (24 — real git targets vs a fake main-apiary tmpdir, gitignore semantics verified with real `git check-ignore`, `:274-320`), `test_doctor.py` (27, including `--fix` through `doctor.main`), `test_apiary_profiles.py` (24), `test_context_rules.py` (25), `utils/test_state.py` (23), `utils/test_jsonc.py` (18), `test_sanitizer.py` (21, includes the source-hygiene property), `test_secret_patterns.py` (10) + `hooks/test_pre_push_secret_scan.py` (21 — `scan_diff`/`iter_added_lines` line-number tracking), `hooks/test_context_rule_error_reminder.py` (19), `hooks/test_learnings_inject.py` (19 — pure scoring).

**Thin:** `test_drift.py` (6 — no malformed-uid case, no "self-pointer uid ≠ registry uid" case, nothing asserting once-per-session semantics), `test_cascade.py` (3), `test_self_bootstrap.py` (4), `test_mailbox.py` (9 — no missing-field message, which is Bug 3), `test_uninstall.py` (9 — no partial-failure ordering, no main-apiary guard), `test_hooks_lib.py` (8 — no "non-apiary hook containing `/runner/` survives" case, which is Bug 8), `hooks/test_pre_push_doc_conformer.py` (8 — `command_pushes` only; `-C` handling is asserted for *detection* but the repo-resolution mismatch is untested).

**Zero tests:** `core/cli.py` (Bug 1 would have been caught by a single `cli.main(["doctor","pointers","--fix"])`), `hook_context.py`, `session.py`, `startup.py` (`parse_identity`, `run_summary`), `transcript.py`, `hooks/extract_transcript.py`, `hooks/inject_session.py` / `check_install.py` / `startup_hook.py` (only their `APIARY_RUNNER_SUBPROCESS` short-circuit is tested in `test_runner_subprocess_guards.py`), `hooks/per_repo_drift_check.py` (the hook shell), `utils/filelock.py` (no core test; only `scribe/test_year_counters.py` touches it — no contention/timeout test exists anywhere), `utils/project.py`, `config.py`, `git_hooks.py` directly (exercised indirectly via `test_install.py:189-230`), and — notably — the **generated launcher**: `test_install.py:68,81` and `test_uninstall.py:135` assert `launch.py` exists but nothing ever executes it, so `--print-repo-path`, the `APIARY_TARGET_STATE_DIR` derivation (`launcher_template.py:63-73`), and the unreachable-main-apiary path are untested.

**Hermeticity.** Good overall: every writing test uses `tempfile.TemporaryDirectory()`; hook subprocess tests redirect `HOME` *and* `USERPROFILE` (`test_save_transcript.py:22-24`, `test_runner_subprocess_guards.py:26-28`, `test_gui_session_surface.py:32-34`), which is what `Path.home()` honours on Windows. Two fragilities: (a) those helpers copy `os.environ` wholesale, so `CLAUDE_PROJECT_DIR`/`APIARY_TARGET_STATE_DIR`/`APIARY_MAIN_REPO` inherited from a live Claude session leak into the hook under test (today harmless because the tested paths short-circuit before consulting them); (b) `test_gui_session_surface.py` runs the real `startup_prompt_hook`, which reads real repo docs (`docs/reference/cli-index.md`, `core/commands/apiary-context.md`) — acceptable (tracked files, not user state) but it couples a unit test to doc content via `RULES_MARKER`/`LAUNCHER_MARKER`.

**Tautological / low-value:** `utils/test_state.py:200-214` (three tests for a four-line env getter with no callers); `test_targets.py` patches `resolve_apiary_repo` (fine) but the module it tests is itself redundant.

**Style note:** `code-style.md` says "Use `unittest`. No pytest." The tests are `unittest.TestCase` classes but the project runs them under pytest (dev dependency, `# pragma: no cover` markers, `.pytest_cache`), and `docs/test_check_cli_claims.py` is pytest-style. The standard should say "unittest-style, pytest runner".

---

## 7. Docs vs reality

| Claim | Where | Reality |
|---|---|---|
| "`~/.claude/` — apiary writes nothing here" | `per-repo-install.md:33-36`, `SETUP.md:306`, `PORTABILITY.md:10` | Identity, history, last-session and ~5 flag files per session are written there (S1). |
| `history.json`, `identity-<short>.json`, `transcripts/` live under `.repos/<slug>/sessions/` | `per-repo-install.md:92-95,131-133` | No live writer; on-disk files date from the 2026-05-05 migration. Live writers target `~/.claude` (`save_transcript.py:22-24`, `session.py:80`). |
| "Identity files written by Claude Code itself stay under `~/.claude/`" | `PORTABILITY.md:75` | Apiary writes them (`startup.py:95`). |
| Main-apiary processes the mailbox "on its own session open" | `per-repo-install.md:206-208`, `SETUP.md:281` | Only manual `apiary mailbox` / `doctor mailbox --fix` (Bug 2). |
| `version.json` "compared against `<main-apiary>/VERSION` on every session open. Mismatch prompts the user to run `apiary update`" | `per-repo-install.md:154-156,230-233,241` | No comparison exists in `drift.py`; `read_version` has no callers; there is no `apiary update` verb (`cli.py:136-189`). |
| "The versioned migration runner under `migrations/` chains the upgrade scripts" | `SETUP.md:263` | No runner in `core/`; `migrations/` is an existence sentinel only (`self_bootstrap.py:32`). |
| Drift check runs "on every session open" / "first tool call" | `per-repo-install.md:158-160`, `per_repo_drift_check.py:2` | Runs and writes on every tool call (Bug 9). |
| Doctor has 7 checks | `per-repo-install.md:236-246`, `cli-tools.md:943` | 8 — `stale` (`doctor.py:156-218`) is undocumented in both. |
| "`--fix` actions land in later phases" | `cli-tools.md:121-122`, `doctor.py:3-6` | `--fix` exists for `mailbox`/`pointers` — but is unreachable via `apiary doctor` (Bug 1). |
| "Run `apiary doctor pointers --fix`" | `SETUP.md:336`, `doctor.py:74,122` | Fails with argparse error (Bug 1). |
| Flags at `~/.claude/{name}-enabled` | `README.md:227` | `<repo>/.claude/apiary/flags/<name>-enabled` (`flags.py:23`). |
| `setup.py` — "Unified installer for all tools" | `README.md:337` | Redirect stub that exits 1. |
| Use `core/config.py` for JSON config | `code-style.md` "Reuse core/" table | Zero callers in the repo. |
| Managed zone lives in `~/.claude/CLAUDE.md` | `context_rules.py:4,18,94`, `scripts/install_context_rules.py:2-4,44` | Per-repo installs write `<repo>/CLAUDE.md` (`install.py:328`). |
| `.apiary/pointer` breadcrumb "post-migration" | `.gitignore:9`, `state.py:10-11` | Not in the per-repo-install.md file layout; written only by the lazy path (`state.py:319`), never by `apiary install`. |
| `MIGRATION-PLAN.md §…` as design source | 15+ code comments, `pyproject.toml:22`, `SETUP.md:16` | File does not exist in the tree. |
| "Anything outside this set is preserved as-is on re-run" | `install.py:31-33` | `_apply_profile_permissions` overwrites profile keys (Bug 7). |
| "install and uninstall paths agree on which entries are ours" | `hooks_lib.py:74-75` | They agree — on an over-broad substring rule that also matches user hooks (Bug 8). |

---

## 8. Verdicts

| Component | Verdict | Reason |
|---|---|---|
| `core/cli.py` | improve | Sound dispatch; missing `--fix` forwarding (Bug 1), zero tests, duplicates doctor's report printing. |
| `core/install.py` | improve | Solid, well-tested core; fix profile-key clobbering (Bug 7), uid reconciliation (Bug 4), make CLAUDE.md/bootstrap-state failures `InstallError`. |
| `core/uninstall.py` | improve | Reorder (files first, registry last), add main-apiary guard (Bug 6). |
| `core/self_bootstrap.py` | keep | Small and correct; `_ensure_registry_initialized` is redundant but harmless. |
| `core/utils/state.py` | improve | The right hub, but carries two identity models and dead pin readers; drop `.apiary/pointer` + lazy registration, unify JSON read/write helpers. |
| `core/drift.py` | improve | Keep move/copy logic; apply registry update inline (already under lock), add once-per-session guard, validate uid-1 invariant. |
| `core/mailbox.py` | delete | Provides no isolation the drift hook doesn't already break; never auto-drained; loses messages on malformed input (Bugs 2-3). |
| `core/cascade.py` | keep | Small, correct; add a sanity check that the new path is a real main-apiary (S5). |
| `core/doctor.py` | improve | Good shape; add `pins` check (self-pointer uid vs registry, main-apiary-pointer vs cwd), document `stale`, scrub phase-0 text. |
| `core/targets.py` | delete | Fully overlapped by `doctor registry/unreachable`; `verified_ok` is write-only. |
| `core/hooks_lib.py` | improve | Replace substring-based `is_apiary_entry` with an explicit marker on generated entries (Bug 8); drop the `$HOME/.claude/apiary_launch.py` mode. |
| `core/hooks_factory.py` | improve | Fine as a builder; stop registering `check_install`, `check_install_stop`, and gate `learnings_inject` registration on the flag. |
| `core/launcher_template.py` | improve | Works; delete `render()`; add an executed-launcher test; consider a `-m`-style dispatch to save the second interpreter. |
| `core/hook_context.py` | improve | Normalise `permissionDecision` values per event type; add tests. |
| `core/session.py` | rewrite | `CLAUDE_DIR`-anchored identity/flag paths are the root of S1; move to `<repo>/.claude/apiary/session-tmp/` (already created by install, `install.py:96`) or `.repos/<slug>/sessions/`. |
| `core/startup.py` | improve | `run_summary` is the only live consumer; drop the argparse surface, stop running apiary's `docs/check.py` for other repos, move `HISTORY_PATH`. |
| `core/flags.py` | keep | Correct and tested; swap env precedence. |
| `core/config.py` | delete | Zero callers. |
| `core/transcript.py` | delete | Zero callers. |
| `core/hooks/extract_transcript.py` | delete (or move to `scribe/`) | Not a hook, zero live references. |
| `core/hooks/check_install.py`, `check_install_stop.py` | delete | Dead manifest logic; no-op Stop hook; each costs a process per call. |
| `core/hooks/startup_hook.py` | delete or rewrite | Always empty in the per-repo model (Bug 14); if context-rule drift matters, check `<repo>/CLAUDE.md` from `startup_prompt_hook` instead. |
| `core/hooks/inject_session.py` | keep | Tiny, useful. |
| `core/hooks/startup_prompt_hook.py` | improve | Does the real work; split the six sections into functions, drop the `PROJECT_ROOT` cwd fallback, replace ad-hoc `.apiary/hooks/` logging dir. |
| `core/hooks/per_repo_drift_check.py` | improve | Add once-per-session guard; surface swallowed exceptions to a log. |
| `core/hooks/pre_push_doc_conformer.py` | improve | Honour `-C`; consider an allowlist of repos permitted to run their own conformer (S2). |
| `core/hooks/pre_push_secret_scan.py` | improve | Scan per-commit diffs (or `git log -p base..HEAD`), lower/lengthen entropy gate, scan the pushed ref (S3). |
| `core/secret_patterns.py` | improve | Add the missing vendor prefixes; otherwise the right design. |
| `core/hooks/context_rule_error_reminder.py` | improve | Tighten the string-form heuristic (Bug 15). |
| `core/hooks/learnings_inject_hook.py` | keep | Pure core is good; the shell's `cwd` fallback is wrong. |
| `core/hooks/research_capture_reminder.py` | keep | Clean pattern for a nudge hook. |
| `core/hooks/save_transcript.py` | rewrite | Not crash-safe, writes to `~/.claude`, runs every turn; fold into the per-target session dir. |
| `core/sanitizer.py` | keep (verify premise) | Well-built, but mangles legitimate `../`/`rm -rf` text; confirm the refusal premise or narrow the list. |
| `core/context_rules.py` | keep | Solid parser/renderer; update docstrings. |
| `core/apiary_profiles.py`, `core/utils/jsonc.py` | keep | Exemplary. |
| `core/git_hooks.py` | keep | Correct, including the `core.hooksPath` trap; drop the `_classify` alias once the script stops importing it. |
| `core/utils/filelock.py` | keep (add tests) | Correct minimal lock; untested contention/timeout. |
| `core/utils/project.py` | keep | Used by `startup.py`/scribe; fine. |

---

## 9. Top 10 recommended changes (value/effort ranked)

1. **Forward `--fix` through `apiary doctor` and add `core/test_cli.py`** (S). Fixes the one documented command that cannot run; a cli test would also lock every verb's argv contract. Rationale: Bug 1 is a user-facing wall with a one-line fix.
2. **Delete dead hooks and modules: `check_install.py`, `check_install_stop.py`, `startup_hook.py`, `config.py`, `transcript.py`, `extract_transcript.py`, `targets.py`, `launcher_template.render`, `_APIARY_OWNED_KEYS`** (S). Removes ~600 lines and three interpreter spawns per tool call with no behaviour change (verified by grep that none have live callers or effects).
3. **Stop writing to `~/.claude`: move identity/flag/history files to `<repo>/.claude/apiary/session-tmp/` (already created at `install.py:96` and documented at `per-repo-install.md:130`) and `.repos/<slug>/sessions/`** (M). Makes the documented contract true, ends the unbounded `~/.claude/tmp` growth (3,389 files today), and lets `uninstall` actually clean up.
4. **Replace the mailbox with an inline registry update in `drift._handle_drift`** (M). The hook already holds the registry lock (`drift.py:138`); write `registry[uid]["real_path"]` there, delete `mailbox.py`, `apiary mailbox`, `doctor mailbox`. Removes Bugs 2-3 and a manual step the docs pretend is automatic.
5. **Add a `doctor pins` check and make `install` reconcile `self-pointer.uid` with the registry** (M). Catches Bug 4/5 states before they silently reroute state to `<repo>/.apiary/`; validate uid 1 is a real apiary checkout while there.
6. **Mark generated hook entries explicitly (e.g. `"_apiary": true` or a `# claude-apiary` token in the command) and make `is_apiary_entry` match only that** (S). Ends silent deletion of user hooks (Bug 8) and makes `remove_hooks` exact.
7. **Fix `_apply_profile_permissions` to merge rather than overwrite, honouring `_APIARY_OWNED_KEYS`** (S). Restores the documented "preserve user content" contract (Bug 7).
8. **Harden the secret gate: scan per-commit added lines over `base..HEAD`, scan the ref actually being pushed, and replace the 4.0-bit gate with a length-aware threshold (e.g. ≥3.5 bits for ≥20 chars, or a charset-normalised score)** (M). Closes the add-then-remove leak and the hex/short-value blind spot (S3), which are the two most realistic real-world misses.
9. **Add a once-per-session guard to `per_repo_drift_check.py` and stop rewriting `self-pointer.json` on the no-drift path** (S). Removes a write + a Windows race per tool call (Bug 9) and matches the documented semantics.
10. **Collapse the duplicated helpers into `core/utils/`: one `git_root(start)`, one `read_json_object(path)`, one `write_json_atomic(path, obj)`, one `now_iso()`, one `MAIN_APIARY_UID`** (M). Eight copies of git-root resolution and seven hand-rolled atomic writers is where the next inconsistency will come from; the change is mechanical and fully covered by existing tests.

Honourable mentions (not in the top 10 by value/effort): wrap `save_transcript.main` in the standard fail-open try/except and make it idempotent per session (S); honour `git -C` in both push gates (S); scrub inherited `APIARY_*`/`CLAUDE_PROJECT_DIR` env in the hook test helpers (S); execute the generated launcher in a test (S); delete every `MIGRATION-PLAN.md §…` reference and the phase-0 prose in `doctor.py` (S); document `doctor stale` (S).
