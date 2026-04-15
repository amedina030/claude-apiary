---
type: standard
title: Schema Migration
scope: project
description: How to bump a runner stage-artifact schema version without breaking in-flight work
framework_version: "1.0"
last_verified: 2026-04-15
---

# Schema Migration

The runner's stage artifacts (spec, plan, execution, harden, report) carry a `schema_version: int` field and are validated via `runner.schema_versions.assert_schema_version`. This doc specifies the procedure for changing one of those schemas without breaking existing artifacts, in-flight runs, or the planner prompt.

## When this applies

Any change to the shape or semantics of a stage artifact — adding a required field, renaming a field, changing a field's type, tightening validation (e.g. making `post_conditions` required), adding a new enum value that older consumers won't recognize.

Non-breaking changes (adding an optional field that old consumers can ignore) do not require a version bump.

## The transition-window pattern

The mechanism is already in `runner/schema_versions.py`: `assert_schema_version` accepts either a single int or a set. During a migration window the consumer declares `supported={N, N+1}`, accepting both old and new artifacts.

Required steps for any breaking change:

1. **Bump the constant** in `runner/schema_versions.py` (e.g. `PLAN_SCHEMA_VERSION = 2`).
2. **Update the producer** (`runner/auto_plan.py`, `runner/auto_refine.py`, etc.) to emit the new shape. The producer always emits the *current* version — never the old one.
3. **Widen the consumer** to `supported={N, N+1}` and branch on `schema_version` to interpret each shape. Keep this widening in place for the full transition window.
4. **Update the stage prompt** (e.g. `build_prompt` in `auto_plan.py`) in the same commit as step 2 so the LLM emits the new shape. Prompt and producer shape must stay in lockstep.
5. **Update the validator** (`runner/validate_plan.py`) to accept both shapes for the window, or to accept only the new shape if step 6 is done.
6. **Close the window** — one of:
   - Re-run `auto_plan` on every queued/in-flight plan so the on-disk corpus is uniformly at version N+1, then narrow the consumer back to `supported=N+1`.
   - Or let the window age out naturally if the old plans are discarded on completion, then narrow the consumer in a later commit.

## What counts as "in-flight"

Any artifact written to disk under `runner/plans/`, `runner/specs/`, `runner/executions/`, `runner/hardens/`, or `runner/reports/`. A plan that was written by version N's producer and has not yet been consumed by the next stage is in-flight and must either be accepted by the widened consumer or regenerated.

## Example

Making `post_conditions` required on plan steps:

- Bump `PLAN_SCHEMA_VERSION` to 2.
- Update `auto_plan.py` prompt to mandate `post_conditions` on every create/modify/delete step.
- `validate_plan.py` requires `post_conditions` only when `plan['schema_version'] >= 2`; version-1 plans keep the optional rule.
- Any downstream consumer that trusts `post_conditions` uses `supported={1, 2}` and treats missing `post_conditions` as empty on version 1.
- After the backlog of v1 plans drains (or is re-planned), drop v1 support in a follow-up commit.

## Anti-patterns

- **Do not** bump the constant without widening consumers — every in-flight v1 plan immediately 500s on read.
- **Do not** update the prompt without bumping the constant — the producer will start emitting the new shape under the old version label, and consumers can't tell the difference.
- **Do not** leave the transition window open forever. Track the migration with a todo note and close it in a follow-up commit.
