---
type: reference
title: Config Files
scope: project
description: Every config file, with the key tables generated from the shipped JSON
framework_version: "1.0"
last_verified: "2026-09-11"
---

# Config Files

## runner/config.json

Runner stage settings. Located in the repo at `runner/config.json`. Every caller
reads it through `runner/config_loader.py`'s `get(section, key, default)`, so a
key the file omits falls back to the default the caller passes — the file is
optional in full.

The Section / Field / Type / Default columns below are **generated from the
shipped `runner/config.json`** by `docs/generate_reference.py`; only the
Description column is hand-written. Change a value in the file and
`--check` fails until the table is regenerated.

<!-- generated:start: config:runner/config.json -->
| Section | Field | Type | Default | Description |
|-------|-----|----|-------|-----------|
| `refine` | `max_retries` | int | `3` | Retries the refiner gets to produce a valid spec |
| `refine` | `model` | string | `"opus"` | Claude model alias for the refiner stage |
| `refine` | `timeout` | int | `900` | Per-attempt timeout in seconds |
| `plan` | `max_retries` | int | `3` | Retries the planner gets to produce a valid plan |
| `plan` | `model` | string | `"opus"` | Claude model alias for the planner stage |
| `plan` | `timeout` | int | `900` | Per-attempt timeout in seconds |
| `executor` | `model` | string | `"sonnet"` | Claude model alias for the executor stage |
| `executor` | `max_retries_per_step` | int | `2` | Retries per execution step |
| `executor` | `max_no_change_retries` | int | `2` | Retries allowed when a step returns without touching a file — the "the model did nothing" guard |
| `executor` | `max_feedback_retries` | int | `2` | Retries a step gets when its result fails a check the model can act on — an unmet post-condition or a commit the repo's hooks rejected — with the failure text fed back; one shared budget per step |
| `executor` | `timeout` | int | `900` | Per-step timeout in seconds |
| `executor` | `mode` | string | `"per_step"` | `per_step` runs one Claude call per plan step; `monolithic` runs the whole plan in one call (and uses `monolithic_executor.timeout_seconds`) |
| `monolithic_executor` | `timeout_seconds` | int | `1800` | Timeout for the single call the monolithic executor makes |
| `harden` | `max_rounds` | int | `1` | Attack-defend rounds per run |
| `harden` | `attacker_model` | string | `"opus"` | Claude model alias for the attacker |
| `harden` | `defender_model` | string | `"sonnet"` | Claude model alias for the defender |
| `harden` | `timeout` | int | `900` | Per-round timeout in seconds |
| `orchestrator` | `stage_timeout` | int | `3600` | Wall-clock ceiling for any one stage before the orchestrator kills it |
| `detached` | `token_cap` | int | `10000000` | Per-run token cap in detached (cron) mode; `--token-cap` overrides it |
| `detached` | `max_unreviewed` | int | `5` | Detached runs refuse to start a new ticket once this many branches are waiting for review |
| `detached` | `max_restarts` | int | `3` | How many times a detached run may be restarted after a recoverable failure |
| `detached` | `resume_failed` | bool | `true` | When the backlog is empty, retry the oldest failed intake ticket that still has restarts and artifacts to resume from (`"parked": true` on a ticket opts it out) |
| `runner` | `target_repo` | null | `null` | Default target repo path for runs that name none. `null` means "the repo apiary resolved" |
| `runner` | `banned_tokens` | object | `{"pytest": "use unittest (stdlib) \u2014 see docs/standards/code-style.md", "shell=true": "shell=True is banned \u2014 use list-form subprocess args", "import requests": "external dependencies are banned \u2014 stdlib only", "from requests": "external dependencies are banned \u2014 stdlib only"}` | Lowercase substring → the message the executor prints when a generated diff contains it |
| `runner` | `target_overrides` | object | `{}` | Target-repo path → a partial config that shadows the top-level one for runs against that repo |
| `usher` | `max_files` | object | `{"pass": 5, "warn": 8}` | Ticket-size gate: files touched, `pass`/`warn` thresholds |
| `usher` | `max_subsystems` | object | `{"pass": 2, "warn": 3}` | Ticket-size gate: distinct subsystems touched |
| `usher` | `max_description_chars` | object | `{"pass": 2000, "warn": 4000}` | Ticket-size gate: description length |
<!-- generated:end: config:runner/config.json -->

`runner.banned_tokens` maps a lowercase substring to the message the executor
prints when a generated diff contains it; `runner.target_overrides` maps a
target-repo path to a partial config that shadows the top-level one for runs
against that repo.

## cron_registry/&lt;hostname&gt;.json

Canonical list of scheduled OS-scheduler entries that apiary owns on a given machine. Located at `<apiary-repo>/cron_registry/<hostname>.json` where `<hostname>` is `platform.node()` (sanitised for filesystem use). Each machine has its own file so multi-machine git-sync setups don't collide. Read by `runner/cron_health.py` (`check` and `repair` subcommands).

```json
{
  "entries": [
    {
      "id": "overnight-runner",
      "description": "Nightly detached runner pass",
      "schedule": {"type": "daily", "time": "02:00"},
      "command": ["<python>", "-m", "runner.run", "--detached"],
      "cwd": "<apiary_repo>"
    },
    {
      "id": "compass-nightly-classify",
      "description": "Classify finished compass turn sessions that ended without /wrapup (D-2026-62)",
      "schedule": {"type": "daily", "time": "03:30"},
      "command": ["<python>", "-m", "compass.classify", "--catch-up"],
      "cwd": "<apiary_repo>"
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Stable handle; becomes the suffix after the `\apiary\` scheduler prefix |
| `description` | string | no | Human-readable label shown in the `check` table |
| `schedule.type` | string | yes | `"daily"` is the only type implemented this release |
| `schedule.time` | string | yes | 24-hour `HH:MM` |
| `command` | list of strings | yes | List-form command; rendered with backend-specific quoting at register time. Use the `<python>` placeholder for the interpreter (resolves per-machine to a real Python 3, honoring `APIARY_PYTHON`) rather than a literal `python`/`python3`, which isn't present on every OS. Also supports `<apiary_repo>`. |
| `cwd` | string | no | Working directory; supports the `<apiary_repo>` placeholder |
| `disabled` | bool | no | `true` means the entry must NOT exist in the scheduler; `repair --apply` deletes any matching entry |

## &lt;main-apiary&gt;/.apiary/gui/apiary_gui/launch.json

Claude Code spawn configuration for the GUI. Auto-created on first run from `DEFAULT_LAUNCH` in `gui/theme.py`; hand-edit to persist non-default values.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | string | `"claude"` | Executable spawned as the pty subprocess. Typically `claude`; override only to point at a vendored binary |
| `args` | list of strings | `[]` | Extra flags appended to the spawn argv (before any flags the GUI adds itself, e.g. `--permission-mode acceptEdits` or `--mcp-config`) |
| `cwd` | string | `""` | Default working directory used when no saved tabs exist. Empty string shows the first-run picker |
| `rows` | int | `40` | Pty rows |
| `cols` | int | `120` | Pty cols |
| `permission_mcp` | bool | `false` | Route permission prompts through the structured MCP path (`gui/permission_mcp.py` + loopback HTTP bridge) instead of the TUI-banner scraper. Env `APIARY_PERMISSION_MCP` overrides this when set. See scribe `C-2026-36` |

Unknown top-level keys are dropped on load (whitelist, see `gui/theme.py::load_launch`). To add a field, update `DEFAULT_LAUNCH` first.

## compass/seed_rules.json

The seed of the compass rule table (D-2026-62). Located at `compass/seed_rules.json` in the repo — source, not state. `compass/rules.py build` merges it with `<state-dir>/compass/rules_manual.json` and the classified events into `<state-dir>/compass/rules.md`, which every session reads at startup (and every runner stage as a prompt preamble). See [Compass rule table](../architecture/compass-rules.md).

| Key | Shape | Description |
|-----|-------|-------------|
| `sections` | list of `{id, title, subtitle}` | The three sections in render order: `judgment`, `output`, `anticipation`. The ids are also the classifier's `section` vocabulary |
| `rules` | list of rows | `id` (`J1`, `O3`, …), `section`, `kind` (`principle` \| `specific`), `parent` (the principle a specific row instantiates, else `null`), `rule` (imperative, second person, to Claude), `why` (one clause — the rationale that also says where the rule stops applying), `source` (`seed`), optional `expiry` (`YYYY-MM-DD`) |
| `self_check` | `{title, items[]}` | The checklist rendered at the end of `rules.md`, applied before finalizing a recommendation or report |

Every row is delivered to Claude in every session, so edit deliberately (`compass/test_rules.py` pins the rendered size at about 1,100 tokens). The row ids are the classifier's `rule` vocabulary: renaming one orphans the events already attributed to it.

## budgeter/config.json

Global budgeter configuration. Located in the repo at `budgeter/config.json`.
`monitored_tools` is also what `core/hooks/dispatch.py` turns into the
budgeter hooks' matcher, so changing it changes which tool calls are logged
*and* which ones pay for the hook.

`usage_sample_interval_seconds`, `model_weights` and the two cache factors
feed the usage-limit sampler and the transcript-based `budgeter/bill.py` /
`budgeter/usage_calibrate.py`; the weights are API list-price ratios used only
to compare models (the account has no API key, so nothing here is a price).

The four `price_weight_*` rows are read by `budgeter/report.py --weighted` and are
deliberately **absent from the shipped file** — their defaults live in
`budgeter/report.py` and are generated from there.

<!-- generated:start: config:budgeter/config.json -->
| Section | Field | Type | Default | Description |
|-------|-----|----|-------|-----------|
|  | `monitored_tools` | array | `["Agent", "Bash", "Read", "Write"]` | Tool names the budgeter hooks fire for; also the dispatcher matcher for both budgeter hooks |
|  | `session_warn_soft_tokens` | int | `600000` | Prompt size at which the session-length nudge suggests wrapping up |
|  | `session_warn_hard_tokens` | int | `800000` | Prompt size at which it suggests starting a new session now |
|  | `usage_sample_interval_seconds` | int | `300` | Minimum seconds between two usage-limit samples in `budgeter/data/usage_samples.jsonl`; the Stop hook and the GUI each check the last sample's age before recording |
|  | `usage_warn_five_hour_soft_pct` | int | `75` | 5-hour limit utilization at which the usage nudge suggests preferring the cheaper path |
|  | `usage_warn_five_hour_hard_pct` | int | `90` | 5-hour utilization at which it tells Claude to raise the limit with the user before going further |
|  | `usage_warn_seven_day_soft_pct` | int | `75` | Same soft threshold for the 7-day limit. The model-specific `seven_day_opus` / `seven_day_sonnet` sub-meters are deliberately not warned on, since they move with whichever model is in use |
|  | `usage_warn_seven_day_hard_pct` | int | `90` | Same hard threshold for the 7-day limit |
|  | `usage_warn_max_sample_age_seconds` | int | `1800` | Ignore the newest usage sample once it is older than this. The nudge reads `usage_samples.jsonl` rather than fetching, so a stale sample may describe a window that has since reset |
| `model_weights` | `claude-fable-5-1` | object | `{"input": 10.0, "output": 50.0, "cache_read": 0.25}` | Relative weight per million tokens used by `bill.py` and `usage_calibrate.py` to compare models (API list-price ratios, not a price); `cache_read` is an absolute rate here because Fable 5.1 prices cache reads flat |
| `model_weights` | `claude-fable-5` | object | `{"input": 10.0, "output": 50.0}` | Same table, Fable 5; cache reads fall back to `cache_read_factor` x `input` |
| `model_weights` | `claude-opus-5` | object | `{"input": 5.0, "output": 25.0}` | Same table, Opus 5 |
| `model_weights` | `claude-sonnet-5` | object | `{"input": 2.0, "output": 10.0}` | Same table, Sonnet 5 |
| `model_weights` | `claude-haiku-4-5` | object | `{"input": 1.0, "output": 5.0}` | Same table, Haiku 4.5; keys match by longest prefix, so dated ids such as `claude-haiku-4-5-20251001` resolve here. A model with no entry counts as zero load and is named in the report footer |
|  | `cache_read_factor` | float | `0.1` | Cache-read tokens weigh this fraction of a model's `input` weight unless the model sets an absolute `cache_read` |
|  | `cache_write_factor` | float | `1.25` | Cache-write tokens weigh this multiple of a model's `input` weight |
|  | `price_weight_input` | float | `1.0` (code default; not in the file) | Weight applied to input tokens by `report.py --weighted` |
|  | `price_weight_cache` | float | `0.1` (code default; not in the file) | Weight applied to cache-read tokens |
|  | `price_weight_cache_creation` | float | `1.25` (code default; not in the file) | Weight applied to cache-creation tokens |
|  | `price_weight_output` | float | `5.0` (code default; not in the file) | Weight applied to output tokens |
<!-- generated:end: config:budgeter/config.json -->

## .claude/budgeter.json (per-project)

Optional per-project budgeter override. Hand-authored — the global install flow that created it was retired in the per-repo migration. Same schema as `budgeter/config.json`. `logger.load_config` reads whichever single file applies — there is no merge with `budgeter/config.json`, so a per-project file must restate every key it wants. Its presence also redirects the log and baseline paths into `<project>/.claude/`.

## prose/config.json

Prose linter defaults. Read by `prose/engine.py`'s `load_config`, which
shallow-merges `<repo>/.claude/prose.json` on top when that file exists (a
list in the override replaces the list below it, so a repo can narrow the
checked paths or disable rules without inheriting).

The Field / Type / Default columns below are **generated from the shipped
`prose/config.json`** by `docs/generate_reference.py`. Only the Description
column is hand-written.

<!-- generated:start: config:prose/config.json -->
| Field | Type | Default | Description |
|-----|----|-------|-----------|
| `styles` | array | `["Tells"]` | Style directories under `prose/styles/` to load rules from |
| `style_paths` | array | `[]` | Extra absolute or repo-relative directories of `*.toml` rules (a per-target extension point) |
| `min_level` | string | `"suggestion"` | Lowest level `check` reports by default |
| `disabled_rules` | array | `[]` | Rule ids to skip |
| `include_globs` | array | `["**/*.md"]` | Paths the hook checks on Write/Edit (fnmatch, repo-relative) |
| `exclude_globs` | array | `[".repos/**", "_tmp_*/**", "node_modules/**", ".venv/**", ".git/**"]` | Paths the hook and `calibrate` skip |
| `context_chars` | int | `40` | Characters of surrounding text kept on each side of a finding |
<!-- generated:end: config:prose/config.json -->

## telephone/config.json

Per-mode limits and tool grants for a cross-repo call, read by
`telephone/store.py`'s `load_config`. A missing or malformed file falls back to
the in-module `DEFAULT_CONFIG`, and a file that sets only part of a mode block
keeps the shipped values for the rest. There is no per-repo override: the caps
exist to bound what an unattended session in another repo can do, so a repo
cannot raise its own.

The Field / Type / Default columns below are **generated from the shipped
`telephone/config.json`** by `docs/generate_reference.py`. Only the Description
column is hand-written.

<!-- generated:start: config:telephone/config.json -->
| Section | Field | Type | Default | Description |
|-------|-----|----|-------|-----------|
|  | `max_autonomous_calls_per_session` | int | `3` | Calls one session may place without the user typing `/telephone`. The count lives in a `session-tmp` flag file, not in the model's head |
|  | `max_autonomous_exchanges_per_line` | int | `6` | Exchanges one call may reach before a follow-up without a grant is refused and the model is told to bring the thread to the user |
|  | `grant_ttl_seconds` | int | `900` |  |
|  | `model` | string | `""` | Model for every callee run. Empty means the claude CLI's own default. `--model` overrides it per call |
| `answer` | `timeout_seconds` | int | `600` | Wall-clock limit for an answer-mode run before it is killed and the record is marked `timed_out` |
| `answer` | `max_turns` | int | `30` | Agentic turns the callee gets, passed as `--max-turns` |
| `answer` | `permission_mode` | string | `""` | Empty sends no `--permission-mode`, so the callee's only grants are the ones below |
| `answer` | `allowed_tools` | array | `["Read", "Glob", "Grep", "Bash(git log *)", "Bash(git show *)", "Bash(git status *)", "Bash(git diff *)", "Bash(python * scribe/notes.py *)"]` | Read-only exploration. The callee's own `permissions.allow` does not apply in an untrusted workspace, so a headless call gets what it needs here |
| `answer` | `disallowed_tools` | array | `["Write", "Edit", "NotebookEdit", "Bash(git push *)", "Bash(git push:*)", "Bash(git commit *)", "Bash(git commit:*)", "Bash(gh pr merge *)", "Bash(gh pr create *)"]` | A deny at any level beats an allow at every other level, so answer mode cannot write, commit, push or open a pull request |
| `act` | `timeout_seconds` | int | `1800` | Wall-clock limit for an act-mode run |
| `act` | `max_turns` | int | `150` | Agentic turns the callee gets, the same ceiling the runner's executor uses |
| `act` | `permission_mode` | string | `"acceptEdits"` | Sent as `--permission-mode`, because a headless run has no one to answer a prompt |
| `act` | `allowed_tools` | array | `["Read", "Edit", "Write", "Glob", "Grep", "Bash(git *)", "Bash(python *)", "Bash(poetry *)", "Bash(pytest *)"]` | File tools plus the command families an act call needs to branch, test and commit |
| `act` | `disallowed_tools` | array | `["Bash(git push *)", "Bash(git push:*)", "Bash(gh pr merge *)", "Bash(gh pr create *)"]` | An unattended session in another repo must never publish. The CLI also compares the callee's remote tracking refs before and after, and records `issue: push detected` when one moved |
<!-- generated:end: config:telephone/config.json -->

## .claude/prose.json (per-project)

Optional per-repo override for the prose linter, same keys as `prose/config.json`. Hand-authored. Unlike `.claude/budgeter.json` it **is** merged: only the keys present override the shipped defaults. The usual uses are `disabled_rules` and a narrower `include_globs`.

## .claude/settings.json

Claude Code settings file at `<repo>/.claude/settings.json`. Hook entries are generated by `core/install.py` (via `core/hooks_factory.py`) — do not edit them manually.

Contains:
- `hooks` — registered PreToolUse, PostToolUse, and Stop hooks
- `permissions` — tool permission rules
- Other Claude Code settings

## core/config/session-registry.json

Registered session roles and missions. Used by `core/session.py` to validate session identity.

```json
{
  "roles": ["user", "attacker", ...],
  "missions": ["general", "project-x", ...]
}
```

## .secretsallow

Per-repo allowlist for both secret-scanning gates — `scripts/secret_scan.py`
at commit time and `core/hooks/pre_push_secret_scan.py` at push time. Lives at
the repo root and is committed, so the exemption travels with the repo. One regex per line; blank
lines and lines starting with `#` are ignored. A plain entry is tested against
the repo-relative **path** and exempts that whole file; an entry prefixed
`line:` is tested against the offending **line** instead and exempts matching
lines anywhere. (Earlier versions tested every entry against both, so a loose
path regex silenced any line containing that word.) An invalid regex is
skipped with a warning rather than failing the scan.

```
# The scanner's own pattern table and fixtures are credential-shaped by
# definition; scanning them would block every commit that touches the feature.
^scripts/secret_scan\.py$
^scripts/test_secret_scan\.py$
```

Prefer the inline `apiary:allow-secret` pragma for a one-off line — an entry
here exempts an entire file from every pattern, which is a wider hole than it
looks. `git commit --no-verify` remains the last-resort bypass.
