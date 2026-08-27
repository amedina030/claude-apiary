# Documentation Index

Framework version: **1.0** | Last updated: 2026-04-02

## Reference

- [CLI Tools](reference/cli-tools.md) — Python CLI entry points, subcommands, and flags
- [CLI Tools Index](reference/cli-index.md) — Quick-reference table of all CLI tools
- [Slash Commands](reference/slash-commands.md) — All slash commands and when to use them
- [Hooks](reference/hooks.md) — All hooks, lifecycle events, and what each does
- [Config Files](reference/config-files.md) — Configuration and state files
- [File Storage](reference/file-storage.md) — Runtime data locations and paths
- [Legacy Scribe Format](reference/legacy-scribe-format.md) — Sanitized .scribe/ format reference for the legacy importer

## Architecture

- [System Overview](architecture/system-overview.md) — Component map and data flow
- [Hook Lifecycle](architecture/hook-lifecycle.md) — PRE-to-PRE delta pattern and agent handling
- [Per-Repo Install Model](architecture/per-repo-install.md) — Pin model, drift detection, cascade-fix

## Standards

- [Code Style](standards/code-style.md) — Naming, structure, testing patterns
- [Doc Style](standards/doc-style.md) — How to write docs for this project
- [New Tool Checklist](standards/new-tool-checklist.md) — What a new tool needs
- [Report Style](standards/report-style.md) — How to write acceptance, validation, and post-mortem reports
- [Schema Migration](standards/schema-migration.md) — How to bump a runner stage-artifact schema version

## Reviews

- [Deep Review 2026-08 (LLM edition)](review/review-for-llm.md) — Repo-wide assessment with file:line evidence, verdicts, and a phased remediation plan for an LLM executor
- [Deep Review 2026-08 (plain-language edition)](review/review-for-human.md) — The same assessment for a person: what's good, what's broken, what to keep/drop, what to decide
- [Core subsystem review](review/subsystems/core.md) — Appendix: install/registry/drift/doctor, hooks, shared utilities
- [Runner subsystem review](review/subsystems/runner.md) — Appendix: six-stage orchestrator, detached mode, schedulers
- [GUI subsystem review](review/subsystems/gui.md) — Appendix: PyWebView wrapper, pty, transcript tail, permission MCP
- [Knowledge tools review](review/subsystems/knowledge.md) — Appendix: scribe, compass, researcher, captures, refiner
- [Budgeter, harden, incubator review](review/subsystems/budgeter-harden-incubator.md) — Appendix: budgeter hooks/estimator, harden validators and prompts, incubator
- [Infra, docs, and skills review](review/subsystems/infra-docs-skills.md) — Appendix: scripts, docs framework, hygiene, secret scanning, all skills

## Guides

- [Adding a Hook](guides/adding-a-hook.md) — End-to-end: write, register, test
- [Adding a Command](guides/adding-a-command.md) — End-to-end: write, register, test
- [Adding a Tool](guides/adding-a-tool.md) — New top-level tool from scratch
- [Bootstrapping a Repo](guides/bootstrapping-a-repo.md) — Apply an apiary profile to a target repo and author new profiles
