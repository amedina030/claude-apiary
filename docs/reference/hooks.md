---
type: reference
title: Hooks
scope: project
description: All registered hooks, their lifecycle events, and what each does
framework_version: "1.0"
last_verified: 2026-08-26
---

# Hooks

Hooks are Python functions that fire at Claude Code lifecycle events. They run as shell commands — no token cost.

Each bootstrapped repo's `.claude/settings.json` registers **one command per event**: the dispatcher, `core/hooks/dispatch.py <verb>`. It reads the event payload from stdin once and runs every relevant hook **in the same process**, then emits one merged response. Individual hooks are no longer separate settings.json entries.

## Hook lifecycle events

| Event | Dispatcher verb | When it fires |
|-------|-----------------|---------------|
| **PreToolUse** | `pre` | Before every tool call |
| **PostToolUse** | `post` | After every tool call |
| **Stop** | `stop` | At the end of every assistant turn (not session end) |
| **UserPromptSubmit** | `prompt` | When the user submits a message |
| **SessionStart** | `session-start` | Session open — verb exists, no hooks registered yet |

## Registered hooks

### Core hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Per-repo drift check | PreToolUse | `core/hooks/per_repo_drift_check.py` | Detects whether the bootstrapped repo has been moved or copied since last bootstrap and updates main-apiary's registry in place, under the lock it already holds. **Once per session** (`SessionId.flag_path("drift_checked")`) — it used to run and rewrite `self-pointer.json` on every tool call. Runs first so the rest of the chain sees an up-to-date self-pointer. Never blocks. |
| Session injector | PreToolUse | `core/hooks/inject_session.py` | Injects `session_id` into hook context. Runs once per session (sets a flag under `<repo>/.claude/apiary/session-tmp/`, skips subsequent calls). Flags are keyed by session_id and safe to persist, so nothing cleans them up at Stop — Stop fires every turn, not at session end, and cleaning up there used to reset the guard (T-2026-117). |
| Learnings injector | PreToolUse | `core/hooks/learnings_inject_hook.py` | Injects the top-3 most-relevant learnings before Edit/Write/Bash, scored against the tool call payload (file paths, command text, tags). Matcher `Edit\|Write\|Bash`. Gated by the `learnings-inject` flag. |
| Research-capture reminder | PreToolUse | `core/hooks/research_capture_reminder.py` | On the first `WebSearch`/`WebFetch`/subagent (`Agent`/`Task`) call of a session, injects a one-time nudge to persist durable findings via the researcher rather than leaving them only in chat. Matching the subagent tool catches research run *inside* a subagent (fires in the parent at spawn). Once per session, keyed on session_id. |
| Pre-push doc-conformer gate | PreToolUse | `core/hooks/pre_push_doc_conformer.py` | On a Bash `git push`, runs the pushed repo's `docs/check_cli_claims.py` and **blocks the push** (with the drift report as the reason) if it exits nonzero. No-op unless that repo ships the conformer, so it's inert in target repos. Fails open on any internal error — only a clean nonzero conformer exit blocks. |
| Pre-push secret-scan gate | PreToolUse | `core/hooks/pre_push_secret_scan.py` | On a Bash `git push`, works out what is being pushed (the named ref, every branch for `--all`/`--mirror`, else `HEAD`; honours `git -C <dir>`), then scans the added lines of **every outgoing commit individually** — commits reachable from that ref but from no ref on the target remote — so a secret committed and later deleted is still caught. Rules come from `core/secret_patterns.py`, the same table the commit-time gate uses. **Blocks the push** on a hit, reporting `file:line @commit` with the value redacted. Append `apiary:allow-secret` or `pragma: allowlist secret` to a line to whitelist an intentional fixture, or list the file in the repo-root `.secretsallow`. Fails open only on internal errors *before* the scan; a scan that starts but does not complete — timeout, git error — **blocks** and says so. |
| Context-rule error reminder | PostToolUse | `core/hooks/context_rule_error_reminder.py` | On Bash failure (non-zero exit, traceback, interrupted, is_error), injects the `recover_from_trivial_errors` behavioral rule and the `Errors Signal Doc Gaps` principle. Skips successes and hook denials. |
| Transcript saver | Stop | `core/hooks/save_transcript.py` | Records the session in `<state-dir>/sessions/{history.json,last-session.json}` for handoff generation, and sweeps stale session files. |
| Startup context injector | UserPromptSubmit | `core/hooks/startup_prompt_hook.py` | Injects identity, notes summary, learnings index, CLI reference, the apiary toolkit rules and the compass profile on the first user message. When `APIARY_GUI_SESSION=1` (set by the GUI at spawn), also injects a `surface:` note telling the session it's running inside the GUI. |

### Budgeter hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Pre-tool cost logger | PreToolUse | `budgeter/hooks/pre_tool_use.py` | Logs the previous tool call's token cost (PRE-to-PRE delta) and injects the session-length nudge once per tier. Matcher is the `monitored_tools` alternation from `budgeter/config.json`. |
| Post-tool agent logger | PostToolUse | `budgeter/hooks/post_tool_use.py` | Logs exact subagent token cost from `tool_response.totalTokens` (Agent calls only). |
| Stop session cleanup | Stop | `budgeter/hooks/stop_session.py` | Logs the final tool call's cost, cleans up temp baseline files. |

### Docs hooks

| Hook | Event | File | Description |
|------|-------|------|-------------|
| Standards reminder | PreToolUse | `docs/hooks/remind_standards.py` | On Write/Edit of `.py` or `docs/*.md` files, injects a one-line reminder to consult the relevant standards doc. Once per file category per session. |

## Hook execution order

Order is the dispatcher's registry (`core.hooks.dispatch._registry`), not settings.json — drift check first, then core, budgeter, docs. A hook whose matcher does not match the payload's `tool_name` is never imported.

PreToolUse (`dispatch.py pre`):

1. `per_repo_drift_check.py` — registry catch-up if the repo moved (all tools, once per session)
2. `inject_session.py` — adds session context (all tools, first call only)
3. `learnings_inject_hook.py` — relevant learnings (`Edit|Write|Bash`)
4. `research_capture_reminder.py` — capture nudge (`WebSearch|WebFetch|Agent|Task`, once per session)
5. `pre_push_doc_conformer.py` — doc-drift push gate (`Bash`)
6. `pre_push_secret_scan.py` — secret push gate (`Bash`)
7. `budgeter/hooks/pre_tool_use.py` — logs cost, session-length nudge (`monitored_tools`)
8. `docs/hooks/remind_standards.py` — standards reminder (`Write|Edit`)

PostToolUse (`dispatch.py post`):

1. `context_rule_error_reminder.py` — behavioural reminder after a failed `Bash`
2. `budgeter/hooks/post_tool_use.py` — logs agent costs (`monitored_tools`)

Stop (`dispatch.py stop`) — fires at the end of every assistant turn, not session end:

1. `budgeter/hooks/stop_session.py` — logs final cost, cleans temp files
2. `save_transcript.py` — records the session

UserPromptSubmit (`dispatch.py prompt`):

1. `startup_prompt_hook.py` — injects the opening context block on the first user message

## The dispatcher

`core/hooks/dispatch.py {pre|post|stop|prompt|session-start}` is the only hook command in settings.json.

**Why.** One settings.json entry per hook meant one launcher process *plus* one script process per hook: a `Bash` tool call fired 7 PreToolUse + 2 PostToolUse hooks — about 18 interpreter starts, ~1.7 s — roughly half of them no-ops that read the payload, saw a tool name they did not care about, and printed `{}`. The dispatcher plus the launcher's in-process `runpy` (below) makes that **two interpreter starts per tool call, ~0.3 s** (measured on the same synthetic `Bash` payload: PreToolUse 1418 ms → 167 ms, PostToolUse 335 ms → 154 ms).

**The contract.** Every hook module exposes:

```python
def run(payload: dict) -> HookResult | None: ...
```

`HookResult` lives in `core/hook_context.py`. Return `None` for "no opinion" (the common case). `HookResult(context=...)` adds text to the single `additionalContext` block the dispatcher emits. `HookResult(block_reason=...)` is a gate's decision to stop the call: the dispatcher turns it into the `deny` JSON + exit 2 that `hook_context.hook_block` emits, and skips every hook after it. **A hook can never vote `allow`** — the contract has no way to express one, which is what keeps default-mode permission prompts alive (review C-1).

Every hook also keeps a standalone shim so `python <hook>.py` still works (tests, debugging, a settings.json entry that predates the dispatcher):

```python
if __name__ == "__main__":
    run_standalone(run)          # or run_standalone(run, event="PostToolUse")
```

**Isolation.** Each hook runs inside its own `try/except`. A hook that raises is logged and the chain continues — one broken hook cannot wedge a session.

**Matchers.** The per-entry `matcher` regex is gone from settings.json (every entry uses `""`), so the dispatcher re-applies it in-process against `tool_name`. Empty / missing / `*` matches every tool; anything else is fullmatched as a regex. A hook whose matcher does not match is not imported at all — that is what keeps a `Read` call cheap.

**Adding a hook.** Write `run(payload)` in a new module, add one `Hook(name, module, matcher)` row to `_registry()` in the right event's tuple, and that is the whole registration — no settings.json change, no re-bootstrap. `core/hooks/test_dispatch.py` asserts every registered module resolves and exposes `run`.

## The hook log

Hook failures are appended to `<repo>/.claude/apiary/hooks.log`:

```
2026-08-26T22:14:33Z PreToolUse learnings_inject: KeyError('year')
Traceback (most recent call last):
  ...
```

One line plus the traceback per failure, rotated to `hooks.log.1` at 1 MiB (one generation kept). An unparseable payload is logged under the hook name `<payload>`.

This exists because the old fan-out swallowed hook errors (`except Exception: pass` per hook) — a hook could be dead for months and nothing said so. The dispatcher still fails open, but no longer silently. The log is the first place to look when a hook "stopped working": if it is empty, the hook ran and chose to say nothing.

## Hook registration

`apiary install --target <repo>` writes `<repo>/.claude/settings.json` from `core/hooks_factory.build_dispatch_hooks()`: one entry per event, empty matcher, command

```
python "$CLAUDE_PROJECT_DIR/.claude/apiary/launch.py" core/hooks/dispatch.py <verb>
```

The launcher (`core/launcher_template.py`) resolves main-apiary from `<repo>/.claude/apiary/main-apiary-pointer.json`, exports `APIARY_MAIN_REPO` / `APIARY_TARGET_REPO` / `APIARY_TARGET_STATE_DIR`, and runs the target **in its own process** via `runpy.run_path` — not as a second interpreter. The target's exit code is propagated, so an exit-2 gate still blocks; an uncaught crash in the target exits 1, never 2.

See `core/hooks_factory.py` for the entry builder, `core/hooks_lib.py` for the registration / detection API, and `core/hooks/dispatch.py` for the registry.

## Utility scripts in hooks directories

These scripts live under `hooks/` directories but are not registered as Claude Code hooks. They are invoked directly by other tools.

| Script | File | Called by | Description |
|--------|------|-----------|-------------|
| Transcript extractor | `core/hooks/extract_transcript.py` | Startup agent | Extracts user + assistant messages from raw transcript JSONL. Usage: `python extract_transcript.py <path>` |
