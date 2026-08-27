---
type: reference
title: CLI Tools Index
scope: project
description: Generated one-line index of every CLI entry point — use cli_lookup.py for full details
framework_version: "1.0"
last_verified: "2026-08-27"
---

# CLI Tools Index

Injected into every session by `core/hooks/startup_prompt_hook.py`. Look up a
tool's full usage with:

```bash
python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" docs/reference/cli_lookup.py <tool>
```

The table below is **generated** from each tool's argparse by
`docs/generate_cli_docs.py`; the Purpose column is hand-written and preserved.
Run `python docs/generate_cli_docs.py --write` after adding or renaming a
flag — `--check` runs in `docs/hooks/pre-commit` and in CI.

<!-- generated:start: cli-index -->
| Tool | Purpose | Subcommands / Key flags |
|----|-------|-----------------------|
| `python scribe/notes.py` | Note and learning management | add, archive, archive-learning, backfill-brief, backup, defer, done, drop, get, learn, learnings, list, … (+12) |
| `python scribe/backup_indexes.py` | Snapshot scribe indexes with retention pruning | --project, --retain |
| `python core/startup.py` | Session initialization and summary | init, summary |
| `python core/doctor.py` | Read-only consistency checks for the per-repo install model. | compass, duplicates, orphans, pins, pointers, registry, stale, unreachable, versions |
| `python core/flags.py` | Per-repo feature flag toggles | disable, enable, status, toggle |
| `python budgeter/report.py` | Usage reporting | --all, --by-agent, --by-request, --by-turn, --date, --flat, --grouped, --since, --weighted |
| `python budgeter/query_request.py` | Sum tokens for a given request_id | --cwd, --request-id |
| `python budgeter/log_agent_cost.py` | Log background agent token costs | --agent, --cwd, --request-id, --session-id |
| `python compass/observations.py` | Inspect/maintain personality observation files | archive, count, list, validate |
| `python compass/capture.py` | Validate + store a /wrapup compass observation payload | dimensions, store, template, validate |
| `python compass/synthesize.py` | Synthesize personality.md from active observations | --cron, --dry-run, --max-sessions, --model |
| `python compass/backfill.py` | Extract observations from historical transcripts | --force, --last, --model, --session-ids, --since |
| `python compass/evaluate.py` | Measure whether the personality profile carries signal | ab, labels, offline |
| `python incubator/cli.py` | Spawn a new side-project repo wired up with apiary | spawn, verify |
| `python researcher/cli.py` | Manage structured research findings (apiary researcher subsystem). | add, find, list, register-tag, show, verify |
| `python captures/cli.py` | Manage visual captures (apiary captures subsystem). | add, find, list, path, register-tag, show |
| `python harden/orchestrate.py` | /harden control flow: plan, prompts, worktree, retry policy, budget, todos | budget, file-todos, plan, prompt, round, save-summary, validate, worktree |
| `python harden/validate_and_assign.py` | Validate + assign IDs in one step | consolidation, findings, response |
| `python harden/lenses.py` | Harden 7-lens taxonomy | codes, json, list |
| `python harden/validate_consolidation.py` | Validate harden Consolidator output | --check-files, --degrade, --file, --source-ids |
| `python harden/assign_ids.py` | Assign sequential IDs to output | --file, --prefix |
| `python harden/validate_findings.py` | Validate Attacker output | --check-files, --deep, --file, --lens, --sanitize |
| `python harden/validate_response.py` | Validate Defender output | --check-files, --expected-ids, --file |
| `python harden/round_counter.py` | Track harden/refine round counts | defender, reset, start, status, tick |
| `python scripts/preflight.py` | Pre-install environment check for claude-apiary. | --gui |
| `python -m runner.run` | End-to-end runner orchestrator | intake |
| `python -m runner.ticket` | Ticket lifecycle: draft, promote, create, bridge, validate | create-intake, draft, from-note, promote, validate |
| `python -m runner.create_intake` | Create runner intake file (shim for `ticket create-intake`) | --context, --description, --explore-hints, --from-todo, --problem, --scope, --title |
| `python -m runner.refine_to_intake` | Bridge refiner scribe note into runner intake/backlog (shim for `ticket from-note`) | --backlog, --explore-hints, --note, --title |
| `python runner/validate_intake.py` | Validate intake JSON | file |
| `python -m runner.auto_refine` | Autonomous refiner (stage 2) | intake |
| `python runner/validate_spec.py` | Validate spec JSON | file |
| `python -m runner.auto_plan` | Autonomous planner (stage 3) | spec |
| `python -m runner.validate_plan` | Validate plan JSON | file |
| `python -m runner.executor` | Code executor (stage 4) | plan |
| `python -m runner.auto_harden` | Autonomous hardener (stage 5) | execution_log |
| `python -m runner.approval` | Approval gate (stage 6) | harden_result |
| `python -m runner.draft_ticket` | Create backlog draft ticket (shim for `ticket draft`) | --context, --description, --from-todo, --problem, --scope, --title |
| `python -m runner.promote` | Promote backlog draft to intake (shim for `ticket promote`) | slug |
| `python runner/mark_done.py` | Mark a backlog ticket as done. | slug |
| `python -m runner.cron_health` | Check or repair the host scheduler's entries against apiary's canonical registry. | check, repair |
| `apiary` | Per-repo install, drift and version tooling (console script over `core/cli.py`) | cascade-fix, doctor, install, self-bootstrap, uninstall, update, version |
| `python core/update.py` | Run pending migrations/ and re-pin bootstrapped repos. | --apiary-repo, --dry-run, --target |
| `python -m runner.monolithic_executor` | Monolithic executor — runner stage 4 (single-subprocess variant) | plan |
| `python -m runner.usher` | Usher — ticket sizing gate | file |
| `python docs/check.py` | Documentation framework conformance checker | --strict |
| `python docs/generate_cli_docs.py` | Generate the CLI reference tables from each tool's argparse | --check, --diff, --write |
| `python docs/generate_reference.py` | Generate the non-argparse reference tables from code | --check, --diff, --write |
| `python docs/change_map.py` | Fail a commit that changes mapped code without its doc | --list, --message, --paths, --staged |
| `python docs/check_cli_claims.py` | Reconcile cli-tools.md claims against real argparse | --only |
| `python scripts/secret_scan.py` | Commit-time secret scanner (stdlib only, no external binaries). | --entropy, --path, --quiet, --staged |
| `python scripts/check_duplicates.py` | AST near-duplicate report for Python function bodies (report-only) | --fail-on-identical, --min-statements, --path, --quiet, --threshold, --top |
| `python scripts/install_git_hooks.py` | Install the secret-scan pre-commit hook into the CURRENT repo. | --force, --list, --repo, --uninstall |
| `python scripts/probe_permission_prompt.py` | Empirical check that hooks don't auto-approve (headless `claude -p`, manual mode) | repo |
| `python scripts/migrate_frontmatter.py` | Reconcile on-disk frontmatter with `core/frontmatter.py` (dry-run default) | --apply, --check, --family, --state-dir, --verbose |
<!-- generated:end: cli-index -->

## Not introspectable

These entries in [cli-tools.md](cli-tools.md) have no single argparse parser to
read, so their rows there are hand-written. `docs/test_generate_cli_docs.py`
asserts this list matches `check_cli_claims.SKIP_HEADERS` exactly, so a tool
cannot quietly join it.

| Entry | Why | Where documented |
|---|---|---|
| `runner/cost_emit.py` | library module — no CLI | [cli-tools.md](cli-tools.md#runnercost_emitpy) |
| `runner/config_loader.py` | library module — no CLI | [cli-tools.md](cli-tools.md#runnerconfig_loaderpy) |
| `gui/app.py` | needs the `gui` poetry group installed | [gui/README.md](../../gui/README.md) |
| `gui/capture_session.py` | needs the `gui` poetry group installed | [cli-tools.md](cli-tools.md#guicapture_sessionpy) |
| `gui/packaging/build.py` | build script, not an argparse CLI | [cli-tools.md](cli-tools.md#guipackagingbuildpy) |
| `gui/packaging/make_icon.py` | build script, not an argparse CLI | [cli-tools.md](cli-tools.md#guipackagingmake_iconpy) |
| `core/hooks/dispatch.py` | one hand-parsed positional verb (argparse would cost an import on the hottest path) | [hooks.md](hooks.md#the-dispatcher) |
| `docs/docgen.py` | library shared by the doc generators | [cli-tools.md](cli-tools.md#docsdocgenpy) |
| `docs/test_doc_examples.py` | pytest module, not a CLI | [cli-tools.md](cli-tools.md#docstest_doc_examplespy) |
| `scripts/install_repo_hooks.py` | no argparse — running it with `--help` installs the git hooks | [cli-tools.md](cli-tools.md#scriptsinstall_repo_hookspy) |
| `Test scripts` | prose category, not a tool | [cli-tools.md](cli-tools.md#test-scripts) |
