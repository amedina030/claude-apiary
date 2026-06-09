---
type: reference
title: Hooks
scope: project
description: All registered hooks, their lifecycle events, and what each does
framework_version: "1.0"
last_verified: 2026-06-09
---

# Hooks

Hooks are Python scripts registered in `~/.claude/settings.json` that fire at Claude Code lifecycle events. They run as shell commands — no token cost.

## Hook lifecycle events

| Event | When it fires |
|-------|--------------|
| **PreToolUse** | Before every tool call |
| **PostToolUse** | After every tool call |
| **Stop** | When the session ends |

## Registered hooks

### Budgeter hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Pre-tool cost logger + warning | PreToolUse | `budgeter/hooks/pre_tool_use.py` | Logs the previous tool call's token cost (PRE-to-PRE delta), evaluates whether the upcoming response looks expensive, injects warning if threshold exceeded |
| Post-tool agent logger | PostToolUse | `budgeter/hooks/post_tool_use.py` | Logs exact subagent token cost from `tool_response.totalTokens` (Agent calls only) |
| Stop session cleanup | Stop | `budgeter/hooks/stop_session.py` | Logs the final tool call's cost, cleans up temp baseline files |

### Core hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Install checker | PreToolUse | `core/hooks/check_install.py` | Verifies installed files match repo manifest. Runs once per session (sets a flag, skips subsequent calls) |
| Install checker cleanup | Stop | `core/hooks/check_install_stop.py` | No-op placeholder (kept for backwards compatibility with existing settings.json). Session-scoped flags persist across turns — Stop fires every turn, not session end, so cleanup here was resetting once-per-session guards (T-2026-117). |
| Session injector | PreToolUse | `core/hooks/inject_session.py` | Injects session identity (session_id, role, mission) into hook context |
| Transcript saver | Stop | `core/hooks/save_transcript.py` | Saves a stripped copy of the session transcript for handoff generation |
| Startup context injector | UserPromptSubmit | `core/hooks/startup_prompt_hook.py` | Injects identity, notes summary, learnings, and CLI reference on the first user message. When `APIARY_GUI_SESSION=1` (set by the GUI at spawn), also injects a `surface:` note telling the session it's running inside the GUI |
| Context-rules drift detector | PreToolUse | `core/hooks/startup_hook.py` | Reports context-rules drift if `~/.claude/CLAUDE.md` has an installed managed zone whose rule hashes diverge from source. Gated by `auto-startup` flag. |
| Context-rule error reminder | PostToolUse | `core/hooks/context_rule_error_reminder.py` | On Bash failure (non-zero exit, traceback, interrupted, is_error), injects the `recover_from_trivial_errors` behavioral rule and the `Errors Signal Doc Gaps` principle. Skips successes and hook denials. |
| Learnings injector | PreToolUse | `core/hooks/learnings_inject_hook.py` | Injects the top-3 most-relevant learnings before Edit/Write/Bash, scored against the tool call payload (file paths, command text, tags). Fail-open on any error path — tool call still proceeds. |
| Research-capture reminder | PreToolUse | `core/hooks/research_capture_reminder.py` | On the first `WebSearch`/`WebFetch`/subagent (`Agent`/`Task`) call of a session, injects a one-time nudge to persist durable findings via the researcher rather than leaving them only in chat. Matching the subagent tool catches research run *inside* a subagent (fires in the parent at spawn). Once per session, keyed on session_id. Fail-open. |
| Pre-push doc-conformer gate | PreToolUse | `core/hooks/pre_push_doc_conformer.py` | On a Bash `git push`, runs the pushed repo's `docs/check_cli_claims.py` and **blocks the push** (with the drift report as the reason) if it exits nonzero. No-op unless that repo ships the conformer, so it's inert in target repos. Fails open on any internal error — only a clean nonzero conformer exit blocks. This is the enforcement half of the doc-conformance loop. |
| Pre-push secret-scan gate | PreToolUse | `core/hooks/pre_push_secret_scan.py` | On a Bash `git push`, scans the *outgoing* diff (commits on `HEAD` but on no remote) and **blocks the push** if an added line contains a high-signal secret — API keys (AWS/GitHub/OpenAI-Anthropic/Slack/Google), private-key blocks, bearer tokens, or a high-entropy credential assignment (`client_secret`/`password`/… with a value clearing a Shannon-entropy gate). Findings report `file:line` with the value redacted so the gate never re-leaks it. Append `pragma: allowlist secret` to a line to whitelist an intentional fixture. Runs in every repo (secret hygiene is universal); fails open on any internal error. |
| Per-repo drift check | PreToolUse | `core/hooks/per_repo_drift_check.py` | Detects whether the bootstrapped repo has been moved or copied since last bootstrap; queues a mailbox message to main-apiary so the registry catches up. Fails silent on errors (must never block tool calls). Installed by `apiary install --target <repo>`. |

## Hook execution order

All PreToolUse hooks fire before every tool call. The order is determined by their position in `settings.json` (managed by `setup.py`). Current order:

1. `inject_session.py` — adds session context
2. `check_install.py` — validates installation (first call only)
3. `pre_tool_use.py` — logs cost, checks for expensive operations
4. `remind_standards.py` — reminds to consult standards (Write/Edit only, once per category per session)

PostToolUse hooks fire after a tool returns:

1. `post_tool_use.py` — logs agent costs

Stop hooks fire at the end of every assistant turn (not session end):

1. `stop_session.py` — logs final cost, cleans temp files
2. `check_install_stop.py` — no-op placeholder
3. `save_transcript.py` — saves transcript

### Docs hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Standards reminder | PreToolUse | `docs/hooks/remind_standards.py` | On Write/Edit of `.py` or `docs/*.md` files, injects a one-line reminder to consult the relevant standards doc. Once per file category per session. |

## Utility scripts in hooks directories

These scripts live under `hooks/` directories but are not registered as Claude Code hooks. They are invoked directly by other tools.

| Script | File | Called by | Description |
|--------|------|-----------|-------------|
| Transcript extractor | `core/hooks/extract_transcript.py` | Startup agent | Extracts user + assistant messages from raw transcript JSONL. Usage: `python extract_transcript.py <path>` |

## Hook registration

Hooks are written into each bootstrapped repo's `<repo>/.claude/settings.json` by `apiary install --target <repo>`. The hook commands all dispatch through the per-repo launcher: `python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" <relative-script-path>`. Each hook entry specifies:
- `matcher`: which tool types trigger this hook (e.g. `Agent`, `Bash`, or `*` for all)
- `hooks`: array of `{type, command}` objects

See `core/hooks_factory.py` for the per-tool builders, `core/hooks_lib.py` for the registration / detection API.
