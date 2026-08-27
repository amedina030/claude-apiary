---
name: harden
description: Adversarial attack-defend loop that stress-tests code or plans
user-invocable: true
---

# /harden — Adversarial Code Hardening

An Attacker agent finds weaknesses, a Defender agent fixes them in a git worktree, and a referee adjudicates when several lenses run at once.

**All control flow lives in `harden/orchestrate.py`.** Path selection, the size cap, the cost estimate, prompt assembly, the retry/degrade policy, the budget threshold and TODO filing are its job. Yours is: call it, spawn the agents it describes, feed their output back, and relay what it prints. When a command prints an `instruction` field, follow it literally — do not invent a different recovery.

Throughout, `ORCH` means:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/orchestrate.py
```

Any `ORCH` command that exits non-zero prints one user-facing line on stderr. Show that line and stop, unless the command's own JSON says otherwise.

## Arguments

- `/harden file1.py [dir/ ...]` — code mode (directories expand recursively; tests and vendored dirs are skipped)
- `/harden --plan <note-id>` — harden a scribe plan/spec note
- `/harden cancel` — run `ORCH round reset --session-id <sid>`, reply "Harden cancelled. No changes were made.", stop

Flags, all optional, all passed straight through to `ORCH plan`: `--lenses`, `--focus`, `--deep`, `--rounds`, `--max-files`, `--max-target-kb`, `--model-attacker`, `--model-consolidator`, `--model-defender`, `--budget-tokens`. Run `ORCH plan --help` for defaults.

## Step 1 — Plan and confirm

```bash
ORCH plan --session-id <sid> --targets <files/dirs> [flags]        # code mode
ORCH plan --session-id <sid> --plan-note <note-id> [flags]         # plan mode
```

Relay its summary block verbatim, including any `WARNING:` line. Then ask the user **in plain prose** whether to proceed or adjust — no multiple-choice picker. On "adjust", re-run `ORCH plan` with the new flags. If they do not say yes, stop: nothing has been created.

## Step 2 — Commit to the run

Only after the user agrees, and in this order:

```bash
ORCH worktree check --session-id <sid>     # refuses if a target is dirty or untracked
ORCH round start --session-id <sid>
ORCH worktree create --session-id <sid>    # code mode; no-op in plan mode
```

## Step 3 — Each round (up to `--rounds`)

Let `N` be the round number, starting at 1.

**3a. Attackers.** `ORCH prompt attacker --session-id <sid> --round N [--prev-findings F --prev-response R --rejections C]` prints one `AGENT` block per lens (or one block on the legacy path). Spawn every block as a foreground `general-purpose` Agent using its exact `description`, `model` and prompt — **all blocks in a single message** so they run in parallel — and never pass `isolation`. Save each agent's raw reply to its own file.

**3b. Validate each attacker.** `ORCH validate findings --file <reply> --session-id <sid> --round N [--lens <name>] --attempt 1`. Exit 0 means the JSON under `result` is validated and `out_file` holds it. Any other exit prints a decision object — do what its `instruction` says, appending its `feedback` to the re-spawned agent's prompt when one is given.

**3c. Referee (multi-lens only).** Merge the per-lens `out_file` arrays into one file, then `ORCH prompt consolidator --session-id <sid> --round N --findings <merged>`; spawn that one agent; then `ORCH validate consolidation --file <reply> --session-id <sid> --round N --source-ids <ids from the merged file> --attempt 1`. Its accepted set is this round's findings. On the single-lens and legacy paths, skip this step — the attacker's findings are the findings.

**3d. No findings.** When the round's findings set is empty: `ORCH round tick --session-id <sid>`, then `ORCH budget check --session-id <sid> --round N --empty-findings`, report `Round N: Attacker found 0 issues. Code/plan looks clean.` plus the returned `suffix`, and jump to Step 4.

**3e. Defender.** Round 1: `ORCH prompt defender --session-id <sid> --round 1 --findings <findings>`, spawn that Agent block, then store its id with `ORCH round defender --session-id <sid> --set <agent_id>`. Round 2+: `ORCH prompt defender-continue --session-id <sid> --round N --findings <findings> --prev-response <prev>` and send that message with **SendMessage** to the id from `ORCH round defender --session-id <sid> --get`. The Defender persists across rounds; if it errors on a continuation, stop the run with "Defender agent failed on round N. Aborting." — do not respawn a fresh one.

**3f. Validate the Defender.** `ORCH validate response --file <reply> --session-id <sid> --round N --expected-ids <ids from this round's findings> --attempt 1`, then handle the decision exactly as in 3b.

**3g. Close the round.**

```bash
ORCH file-todos --session-id <sid> --round N --response <validated> --findings <findings>
ORCH round tick --session-id <sid>
ORCH budget check --session-id <sid> --round N
```

Print `Round N summary: <fixed> fixed, <refactored> refactored, <deferred> deferred` followed by the `suffix` from `budget check`, then follow its `instruction`. Carry this round's findings and validated response files into the next round's 3a and 3e. Stop early when every finding this round was deferred and `N > 1`: "All findings deferred — no further fixes possible. Exiting loop."

## Step 4 — Present results

Code mode: `ORCH worktree diff --session-id <sid>`, then the tally — `**Harden complete.** <N> rounds, <total> findings, <fixed> fixed, <deferred> deferred.` and the branch name. Plan mode: show the last Defender's `amended_spec`.

Ask in plain prose whether to keep the work or discard it. On discard, run `ORCH worktree remove --session-id <sid>` (add `--delete-branch` only if the user asks for the branch gone too) and still save the summary below.

## Step 5 — Save

Write the summary to a file, then `ORCH save-summary --session-id <sid> --content-file <file>` and `ORCH round reset --session-id <sid>`.

```
## /harden Summary

<`BUDGET EXCEEDED` + `Spend at abort: <spent> of <budget>` — only when budget check reported an overrun>
**Target:** <files or note #id>
**Settings:** path=…, lenses=…, focus=…, deep=…, rounds=<completed>/<max>
**Models:** attacker=…, consolidator=…, defender=…

### Round-by-round
#### Round 1
**Lenses run:** <names, noting any the decision object told you to drop>
**Findings:** <accepted> accepted (<rejected> rejected by referee)
- CON-001 [severity] description  ⟵ ATK-SEC-001 (security)  → DEF-001 [fixed] description

**Referee rejections:**
- ATK-CPX-004 — Reason: <why>

### Deferred items
- <finding-id>: <description> — Reason: <why>

### TODOs created
- <the `todos` list from file-todos>
```

Close with, code mode: "Summary saved. Worktree branch: `<branch>` — merge when ready." Plan mode: "Summary saved. Amended spec is in the output above — use it for implementation."
