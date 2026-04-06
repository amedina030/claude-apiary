---
type: reference
title: CLI Tools Index
scope: project
description: Quick-reference table of all CLI entry points with key flags — use cli_lookup.py for full details
framework_version: "1.0"
last_verified: "2026-04-05"
---

# CLI Tools Index

Look up full usage with: `python docs/reference/cli_lookup.py <tool>`

| Tool | Purpose | Subcommands / Key Flags |
|------|---------|------------------------|
| `scribe/notes.py` | Note and learning management | add, list, get, done, update, archive, learn, learnings, unlearn |
| `core/startup.py` | Session initialization and summary | init, summary |
| `budgeter/report.py` | Usage reporting | --date, --since, --flat, --grouped, --by-turn, --by-agent, --weighted, --feedback |
| `budgeter/tune.py` | Suggest rule weight adjustments | --min, --percentile, --yes |
| `budgeter/log_agent_cost.py` | Log background agent token costs | --session-id, --agent, --cwd |
| `clarifier/log_cost.py` | Track clarifier session costs | tally, finalize |
| `clarifier/write_log.py` | Manage clarifier session logs | --append, --complete |
| `refiner/round_counter.py` | Track refinement round counts | start, tick, reset, status |
| `harden/pipeline.py` | Validate + assign IDs pipeline | findings, response |
| `harden/assign_ids.py` | Assign sequential IDs to output | --prefix, --file |
| `harden/validate_findings.py` | Validate Attacker output | --check-files, --deep, --sanitize |
| `harden/validate_response.py` | Validate Defender output | --expected-ids, --check-files |
| `harden/round_counter.py` | Track harden round counts | start, tick, reset, status, defender |
| `pipeline/run.py` | End-to-end pipeline orchestrator | `<intake_path>` |
| `pipeline/create_intake.py` | Create pipeline intake file | --from-todo, --title, --problem, --description, --scope, --context |
| `pipeline/validate_intake.py` | Validate intake JSON | `<file>` |
| `pipeline/auto_refine.py` | Autonomous refiner (stage 2) | `<intake>` |
| `pipeline/validate_spec.py` | Validate spec JSON | `<file>` |
| `pipeline/auto_plan.py` | Autonomous planner (stage 3) | `<spec>` |
| `pipeline/validate_plan.py` | Validate plan JSON | `<file>` |
| `pipeline/executor.py` | Code executor (stage 4) | `<plan>` |
| `pipeline/auto_harden.py` | Autonomous hardener (stage 5) | `<execution_log>` |
| `pipeline/approval.py` | Approval gate (stage 6) | `<harden_result>` |
| `pipeline/config_loader.py` | Shared pipeline config loader | Library — no CLI |
| `setup.py` | Unified installer | --global, --project-path, --check, --with-test-suite |
