---
type: reference
title: CLI Tools Index
scope: project
description: Quick-reference table of all CLI entry points with key flags — use cli_lookup.py for full details
framework_version: "1.0"
last_verified: "2026-04-07"
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
| `budgeter/query_request.py` | Sum tokens for a given request_id | --request-id, --cwd |
| `refiner/round_counter.py` | Track refinement round counts | start, tick, reset, status |
| `harden/validate_and_assign.py` | Validate + assign IDs in one step | findings, response |
| `harden/assign_ids.py` | Assign sequential IDs to output | --prefix, --file |
| `harden/validate_findings.py` | Validate Attacker output | --check-files, --deep, --sanitize |
| `harden/validate_response.py` | Validate Defender output | --expected-ids, --check-files |
| `harden/round_counter.py` | Track harden round counts | start, tick, reset, status, defender |
| `runner/run.py` | End-to-end runner orchestrator | `<intake_path>` |
| `runner/create_intake.py` | Create runner intake file | --from-todo, --title, --problem, --description, --scope, --context |
| `runner/validate_intake.py` | Validate intake JSON | `<file>` |
| `runner/auto_refine.py` | Autonomous refiner (stage 2) | `<intake>` |
| `runner/validate_spec.py` | Validate spec JSON | `<file>` |
| `runner/auto_plan.py` | Autonomous planner (stage 3) | `<spec>` |
| `runner/validate_plan.py` | Validate plan JSON | `<file>` |
| `runner/executor.py` | Code executor (stage 4) | `<plan>` |
| `runner/auto_harden.py` | Autonomous hardener (stage 5) | `<execution_log>` |
| `runner/approval.py` | Approval gate (stage 6) | `<harden_result>` |
| `runner/draft_ticket.py` | Create backlog draft ticket | --title, --problem, --description, --scope, --context, --from-todo (only fills description) |
| `runner/promote.py` | Promote backlog draft to intake | `<slug>` (filename without dir or .json extension) |
| `runner/cost_emit.py` | Emit usage XML from Claude envelope | Library — no CLI |
| `runner/config_loader.py` | Shared runner config loader | Library — no CLI |
| `setup.py` | Unified installer | --global, --project-path, --check |
