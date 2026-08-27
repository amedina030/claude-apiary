---
type: reference
title: CLI Tools Index
scope: project
description: Quick-reference table of all CLI entry points with key flags — use cli_lookup.py for full details
framework_version: "1.0"
last_verified: "2026-08-26"
---

# CLI Tools Index

Look up full usage with: `python docs/reference/cli_lookup.py <tool>`

| Tool | Purpose | Subcommands / Key Flags |
|------|---------|------------------------|
| `scribe/notes.py` | Note and learning management | add, list, get, done, update, archive, tidy, mark-reviewed, learn, learnings, unlearn |
| `scribe/backup_indexes.py` | Snapshot scribe indexes with retention pruning | --retain, --project |
| `core/startup.py` | Session initialization and summary | init, summary |
| `core/flags.py` | Per-repo feature flag toggles | toggle, enable, disable, status |
| `budgeter/report.py` | Usage reporting | --date, --since, --flat, --grouped, --by-turn, --by-agent, --by-request, --weighted |
| `budgeter/log_agent_cost.py` | Log background agent token costs | --session-id, --agent, --cwd |
| `budgeter/query_request.py` | Sum tokens for a given request_id | --request-id, --cwd |
| `compass/observations.py` | Inspect/maintain personality observation files | count, list, validate, archive |
| `compass/synthesize.py` | Synthesize personality.md from active observations | --dry-run, --model, --cron |
| `compass/backfill.py` | Extract observations from historical transcripts | --last, --session-ids, --since, --force, --model |
| `harden/validate_and_assign.py` | Validate + assign IDs in one step | findings, response |
| `harden/assign_ids.py` | Assign sequential IDs to output | --prefix, --file |
| `harden/validate_findings.py` | Validate Attacker output | --check-files, --deep, --sanitize |
| `harden/validate_response.py` | Validate Defender output | --expected-ids, --check-files |
| `harden/round_counter.py` | Track harden/refine round counts | start, tick, reset, status, defender |
| `python -m runner.run` | End-to-end runner orchestrator | `<intake_path>` |
| `python -m runner.create_intake` | Create runner intake file | --from-todo, --title, --problem, --description, --scope, --context |
| `python -m runner.refine_to_intake` | Bridge refiner scribe note into runner intake/backlog | --note, --title, --backlog, --explore-hints |
| `python -m runner.validate_intake` | Validate intake JSON | `<file>` |
| `python -m runner.auto_refine` | Autonomous refiner (stage 2) | `<intake>` |
| `python -m runner.validate_spec` | Validate spec JSON | `<file>` |
| `python -m runner.auto_plan` | Autonomous planner (stage 3) | `<spec>` |
| `python -m runner.validate_plan` | Validate plan JSON | `<file>` |
| `python -m runner.executor` | Code executor (stage 4) | `<plan>` |
| `python -m runner.auto_harden` | Autonomous hardener (stage 5) | `<execution_log>` |
| `python -m runner.approval` | Approval gate (stage 6) | `<harden_result>` |
| `python -m runner.draft_ticket` | Create backlog draft ticket | --title, --problem, --description, --scope, --context, --from-todo (only fills description) |
| `python -m runner.promote` | Promote backlog draft to intake | `<slug>` (filename without dir or .json extension) |
| `python -m runner.cron_health check` | Inspect OS-scheduler drift against `cron_registry/<hostname>.json` | none |
| `python -m runner.cron_health repair` | Fix scheduler drift (dry-run default) | `--apply` |
| `runner/cost_emit.py` | Emit usage XML from Claude envelope | Library — no CLI |
| `runner/config_loader.py` | Shared runner config loader | Library — no CLI |
| `scripts/probe_permission_prompt.py` | Empirical check that hooks don't auto-approve (headless `claude -p`, manual mode) | `<repo>`, --model, --timeout |
| `incubator/cli.py` | Spawn a new side-project repo wired up with apiary | spawn |
