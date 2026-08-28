---
type: reference
title: Hooks
scope: project
description: Every hook the dispatcher runs, in order, with its event and matcher
framework_version: "1.0"
last_verified: 2026-08-27
---

# Hooks

Hooks are Python functions that fire at Claude Code lifecycle events. They run as shell commands — no token cost.

Each bootstrapped repo's `.claude/settings.json` registers **one command per event**: the dispatcher, `core/hooks/dispatch.py <verb>`. It reads the event payload from stdin once and runs every relevant hook **in the same process**, then emits one merged response. Individual hooks are no longer separate settings.json entries.

## Hook lifecycle events

<!-- generated:start: hooks:events -->
| Event | Dispatcher verb | When it fires |
|-----|---------------|-------------|
| **PreToolUse** | `pre` | Before every tool call |
| **PostToolUse** | `post` | After every tool call |
| **Stop** | `stop` | At the end of every assistant turn — **not** session end |
| **UserPromptSubmit** | `prompt` | When the user submits a message |
| **SessionStart** | `session-start` | Session open — the verb exists, no hook is registered yet |
<!-- generated:end: hooks:events -->

## Registered hooks

The table is generated from `core.hooks.dispatch._registry()` by
`docs/generate_reference.py` — the event, order, module and matcher columns are
the dispatcher's, not this document's. Only the last column is hand-written.
Adding a `Hook(...)` row to the registry and running
`python docs/generate_reference.py --write` is the whole documentation step.

<!-- generated:start: hooks:registry -->
| Event | # | Hook | Module | Matcher | What it does |
|-----|---|----|------|-------|------------|
| PreToolUse (`pre`) | 1 | `drift_check` | `core/hooks/per_repo_drift_check.py` | _(every tool)_ | Detects whether the bootstrapped repo has been moved or copied since the last bootstrap and updates main-apiary's registry in place, under the lock it already holds. **Once per session** (`SessionId.flag_path("drift_checked")`). Runs first so the rest of the chain sees an up-to-date self-pointer. Never blocks. |
| PreToolUse (`pre`) | 2 | `inject_session` | `core/hooks/inject_session.py` | _(every tool)_ | Injects `session_id` into hook context. First call of the session only (flag under `<repo>/.claude/apiary/session-tmp/`). Flags are keyed by session id and swept by age, not at Stop — Stop fires every turn, and cleaning up there used to reset the guard (T-2026-117). |
| PreToolUse (`pre`) | 3 | `learnings_inject` | `core/hooks/learnings_inject_hook.py` | `Edit\|Write\|Bash` | Injects the top-3 most-relevant learnings before an Edit/Write/Bash, scored against the tool payload (file paths, command text, tags). Gated by the `learnings-inject` flag. |
| PreToolUse (`pre`) | 4 | `research_reminder` | `core/hooks/research_capture_reminder.py` | `WebSearch\|WebFetch\|Agent\|Task` | On the session's first `WebSearch`/`WebFetch`/subagent call, injects a one-time nudge to persist durable findings via the researcher instead of leaving them in chat. Matching the subagent tool catches research run *inside* a subagent (it fires in the parent at spawn). |
| PreToolUse (`pre`) | 5 | `pre_push_doc_conformer` | `core/hooks/pre_push_doc_conformer.py` | `Bash` | On a Bash `git push`, runs the pushed repo's `docs/check_cli_claims.py` and **blocks the push** with the drift report as the reason if it exits nonzero. Inert in repos that do not ship the conformer. Fails open on internal errors — only a clean nonzero exit blocks. |
| PreToolUse (`pre`) | 6 | `pre_push_secret_scan` | `core/hooks/pre_push_secret_scan.py` | `Bash` | On a Bash `git push`, works out what is being pushed (the named ref, every branch for `--all`/`--mirror`, else `HEAD`; honours `git -C <dir>`), then scans the added lines of **every outgoing commit individually**, so a secret committed and later deleted is still caught. Rules come from `core/secret_patterns.py`, shared with the commit-time gate. **Blocks** on a hit, reporting `file:line @commit` with the value redacted. Whitelist with `apiary:allow-secret` on the line or an entry in `.secretsallow`. A scan that starts but cannot finish blocks and says so. |
| PreToolUse (`pre`) | 7 | `budgeter_pre` | `budgeter/hooks/pre_tool_use.py` | `Agent\|Bash\|Read\|Write` | Logs the previous tool call's token cost (PRE-to-PRE delta) and injects the session-length nudge once per tier. The matcher is the `monitored_tools` alternation read from `budgeter/config.json` at dispatch time. |
| PreToolUse (`pre`) | 8 | `remind_standards` | `docs/hooks/remind_standards.py` | `Write\|Edit` | On the session's first Write/Edit of a `.py` file or a `docs/*.md` file, injects a one-line pointer to the relevant standards doc. Once per file category per session. Classification is relative to **the repo the write is in** (`CLAUDE_PROJECT_DIR`, then `APIARY_TARGET_REPO`, then the payload cwd), so it works in every bootstrapped repo; "new tool" means the top-level directory holds no other Python (T-2026-282). |
| PostToolUse (`post`) | 1 | `context_rule_error_reminder` | `core/hooks/context_rule_error_reminder.py` | `Bash` | On a failed Bash call (non-zero exit, traceback, interrupted, `is_error`), injects the `recover_from_trivial_errors` rule and the `Errors Signal Doc Gaps` principle, and files a scribe todo when the failure is doc-shaped (an unrecognised argument on a documented command, or a documented path that does not exist). Skips successes and hook denials. |
| PostToolUse (`post`) | 2 | `budgeter_post` | `budgeter/hooks/post_tool_use.py` | `Agent\|Bash\|Read\|Write` | Logs exact subagent token cost from `tool_response.totalTokens` (Agent calls only). |
| Stop (`stop`) | 1 | `budgeter_stop` | `budgeter/hooks/stop_session.py` | _(every tool)_ | Logs the final tool call's cost and cleans up the temp baseline file. |
| Stop (`stop`) | 2 | `save_transcript` | `core/hooks/save_transcript.py` | _(every tool)_ | Records the session in `<state-dir>/sessions/{history.json,last-session.json}` for handoff generation, and sweeps stale session files. |
| UserPromptSubmit (`prompt`) | 1 | `startup_prompt` | `core/hooks/startup_prompt_hook.py` | _(every tool)_ | On the first user message, injects identity, the notes summary, the learnings index, the CLI index, the apiary toolkit rules and the compass profile. With `APIARY_GUI_SESSION=1` it also injects a `surface:` note saying the session runs inside the GUI. |
<!-- generated:end: hooks:registry -->

Order matters: the drift check runs first so everything after it sees an up-to-date self-pointer and registry entry, then core, budgeter, docs — the order `core/install.py` used to write into settings.json. A hook whose matcher does not match the payload's `tool_name` is never imported.

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

**Adding a hook.** Write `run(payload)` in a new module, add one `Hook(name, module, matcher)` row to `_registry()` in the right event's tuple, and that is the whole registration — no settings.json change, no re-bootstrap. `core/hooks/test_dispatch.py` asserts every registered module resolves and exposes `run`; `docs/test_generate_reference.py` asserts the table above lists it.

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

## Repo-local git hooks

Separate from Claude Code hooks: `docs/hooks/pre-commit` and `docs/hooks/pre-commit-secret-scan` are POSIX shell git hooks installed into main-apiary's own `.git/hooks/` by `python scripts/install_repo_hooks.py`. `runner/hooks/post-merge` is installed into a runner target. They are documented here because `docs/check.py` looks for every script under a `hooks/` directory.

| Script | Installed by | What it does |
|---|---|---|
| `docs/hooks/pre-commit` | `scripts/install_repo_hooks.py` | Runs `docs/check.py`, `docs/check_cli_claims.py`, both doc generators' `--check` and `scripts/secret_scan.py --staged` on every commit |
| `docs/hooks/commit-msg` | `scripts/install_repo_hooks.py` | Runs `docs/change_map.py --staged --message <file>`: a staged change to a mapped code file must be accompanied by its architecture doc, unless the message carries a `docs: unchanged` trailer (git has not written the message yet when pre-commit runs, so the check lives here; `APIARY_DOCS_UNCHANGED=1` is the non-interactive waiver) |
| `docs/hooks/pre-commit-secret-scan` | `core/git_hooks.py` (every bootstrapped repo) | The secret scan alone, for repos that do not ship the docs framework |
| `runner/hooks/post-merge` | `runner/` setup | Closes the source scribe todo after a runner branch is merged |

## Utility scripts in hooks directories

These scripts live under `hooks/` directories but are not registered as Claude Code hooks. They are invoked directly by other tools.

| Script | File | Called by | Description |
|--------|------|-----------|-------------|
| Transcript extractor | `core/hooks/extract_transcript.py` | Startup agent | Extracts user + assistant messages from raw transcript JSONL. Usage: `python extract_transcript.py <path>` |
