---
name: harden
description: Adversarial attack-defend loop that stress-tests code or plans
user-invocable: true
---

# /harden — Adversarial Code Hardening

Run an automated attack-defend loop where an Attacker agent finds weaknesses and a Defender agent fixes them. Works on code files or plan specs.

## Arguments

- `/harden file1.py [file2.py ...]` — harden one or more code files (max 5 by default)
- `/harden path/to/dir/` — harden all code files in a directory (recursive)
- `/harden --plan <note-id>` — harden a scribe plan/spec note
- `/harden cancel` — cancel the current harden loop and exit

### Optional flags

- `--lenses <list>` — comma-separated subset of the 7-lens taxonomy to run (e.g. `--lenses security,correctness`). **Code mode only.** Default: **all 7 lenses** (`correctness,security,robustness,resilience,complexity,architecture,testing`). One lens → single-lens path (no referee); 2+ lenses → multi-lens fan-out with the consolidating referee.
- `--focus <type>` — **legacy single-attacker path.** focus area: `general` (default), `security`, `input`, `logic`, `complexity`, `resilience`. Providing `--focus` (and NOT `--lenses`) in code mode runs the original single-attacker→defender flow with the legacy category vocabulary, preserving prior behavior. In plan mode, `--focus` is the only path (lenses do not apply to plans).
- `--deep` — require concrete Given/When/Then attack scenarios
- `--rounds N` — max rounds (default 3)
- `--max-files N` — max files allowed (default 5)
- `--model-attacker <model>` — model for Attacker agents (default `sonnet`); applies to every lens attacker
- `--model-consolidator <model>` — model for the Consolidator/referee agent (default `sonnet`)
- `--model-defender <model>` — model for Defender agent (default `sonnet`)
- `--budget-tokens N` — token budget for this run (default 450000); used to gate spend tracking
- `--max-target-kb K` — max total size of target files in KB before aborting (default 50)

### Path selection (which loop runs)

Resolved once in Step 0 after parsing, and it determines Step 2:

- **Plan mode** → always the **legacy path** (single attacker, `--focus`/`general`). Lenses do not apply to plans.
- **Code mode + `--focus` given + no `--lenses`** → **legacy path** (back-compat, category vocab, `ATK-NNN`).
- **Code mode otherwise** → **lens path**, with `resolved_lenses` = `--lenses` value or all 7:
  - exactly **one** lens → **single-lens** path: one lens specialist attacker → defender, **referee skipped** (`ATK-<CODE>-NNN` feeds the defender directly).
  - **two or more** lenses → **multi-lens** path: N parallel lens attackers → consolidator/referee → defender (`ATK-<CODE>-NNN` → `CON-NNN` → `DEF-NNN`).

---

## Step 0: Parse and validate

### Cancel

If the argument is `cancel`:
1. Run: `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py reset --session-id <session_id>`
2. Respond: "Harden cancelled. No changes were made."
3. Stop.

### Parse arguments

Determine the mode from the arguments:

- If `--plan <note-id>` is present → **plan mode**. The note ID is the argument.
- Otherwise → **code mode**. All non-flag arguments are file paths or directory paths.

### Expand directories

After extracting the non-flag arguments, check each one: if it's a directory (use Bash `test -d` or equivalent), expand it into individual files using the Glob tool with the pattern `**/*.{py,js,ts,tsx,jsx,go,rs,java,rb,sh}` rooted at that directory. Replace the directory argument with the expanded file list.

**Exclusions:** Skip files matching these patterns:
- `__pycache__/`, `node_modules/`, `.git/`
- `test_*.py`, `*_test.py`, `*_test.go`, `*.test.ts`, `*.test.js`, `*.spec.ts`, `*.spec.js`

After expansion, deduplicate the full file list (in case a file was listed both explicitly and via directory).

Extract optional flags with their defaults:
- `--lenses`: default unset (resolved below)
- `--focus`: default `general` (track whether it was *explicitly* provided — needed for path selection)
- `--deep`: default `false`
- `--rounds`: default `3`
- `--max-files`: default `5`
- `--model-attacker`: default `sonnet`
- `--model-consolidator`: default `sonnet`
- `--model-defender`: default `sonnet`
- `--budget-tokens`: default `450000`
- `--max-target-kb`: default `50`

### Resolve the path and lenses

Decide which Step 2 loop will run (see **Path selection** above) and persist these variables:

- `path` ∈ {`legacy`, `single-lens`, `multi-lens`}
- `resolved_lenses` — ordered list of lens names (lens path only)

Rules:

1. **Plan mode** → `path = legacy`. Skip the rest of this block.
2. **Code mode + `--focus` explicitly provided + `--lenses` absent** → `path = legacy`. Skip the rest.
3. Otherwise (code mode, lens path): build `resolved_lenses`.
   - If `--lenses` was provided, split on commas and lowercase each entry. Otherwise use all seven from `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/lenses.py list`.
   - Validate every entry against the canonical set (`python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/lenses.py codes` gives `name=CODE` pairs). If any entry is unknown, abort: "Unknown lens `<x>`. Valid lenses: correctness, security, robustness, resilience, complexity, architecture, testing." Do not start the round counter or create a worktree.
   - Deduplicate while preserving order.
   - If `len(resolved_lenses) == 1` → `path = single-lens`. Else → `path = multi-lens`.

Read the lens code map once (`harden/lenses.py codes`) and keep it; you need `code_for(lens)` to build the `--lens` argument and to read back `ATK-<CODE>-NNN` IDs.

### Validate inputs

**Code mode:**
1. Check that at least one file path was provided (after directory expansion). If not, tell the user and stop. If a directory was provided but expansion found 0 matching files, tell the user: "No code files found in `<dir>`. Check the path or add files explicitly."
2. Check that the number of files does not exceed `--max-files`. If it does, abort with: "Too many files (N > max). Narrow scope, use `--max-files N`, or pass specific files instead of a directory."
3. For each file, verify it exists using the Read tool. If any file is missing, list the missing files and stop.

**Plan mode:**
1. Run: `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py get <note-id>`
2. If the note doesn't exist, tell the user: "Note <id> not found. Use `/notes` to find the correct ID." Stop.
3. Save the note content for use in later steps.

### Pre-flight target size check

**Code mode only.** Before starting the round counter or creating any worktree, verify the total size of the resolved target files does not exceed `--max-target-kb`.

Compute total bytes by writing the following Python script to a temp file and running it. Embed the resolved file paths as a list literal — this avoids command-line length limits (Windows 8191-char cap) and quoting issues with paths containing spaces:

```python
import sys
from pathlib import Path

paths = [
    # embed one entry per resolved file — use the actual paths
    r"<resolved_file_1>",
    r"<resolved_file_2>",
    # ...
]

try:
    total_bytes = sum(Path(p).stat().st_size for p in paths)
    print(total_bytes)
except OSError as e:
    print(f"size-check error: {e}", file=sys.stderr)
    sys.exit(1)
```

Write this script to a temp file (e.g. `__harden_size_check.py`) and run `python __harden_size_check.py`. Delete the temp file after reading the output. If the script exits non-zero, abort: "Pre-flight size check failed: <error>." Do not start the round counter or create a worktree.

Then compute:

```
total_kb = ceil(total_bytes / 1024)
```

If `total_kb > max_target_kb`, print:

```
Target size <total_kb> KB exceeds --max-target-kb <max_target_kb>. Narrow scope or raise the cap.
```

And **stop the run immediately** — do not start the round counter, do not create the worktree, do not spawn any Agent. This ensures `usage_log.jsonl` is untouched.

In plan mode, skip this check entirely.

### Generate request_id

Compute a stable, human-readable ID for this harden run:

```bash
# session short prefix
sid_short = <session_id>[:8]   # first 8 characters of the session UUID

# unix timestamp (cross-platform — do not use $(date +%s) directly)
unix_ts = $(python -c "import time;print(int(time.time()))")

# 4-char random hex suffix — guards against same-second collisions from concurrent runs
rand_hex = $(python -c "import secrets;print(secrets.token_hex(2))")

# assemble
request_id = harden-<sid_short>-<unix_ts>-<rand_hex>
```

Persist `request_id` as a local variable for use throughout the loop (Step 2 Attacker and Defender spawns).

Also capture the session working directory (the directory Claude Code is running in when `/harden` is invoked) as `session_cwd`. This is used when querying per-request spend so that the log read targets the same project log that `post_tool_use.py` wrote to.

**IMPORTANT:** The Agent tool has no `env` parameter, so `APIARY_REQUEST_ID` cannot be injected by the LLM via environment. Instead, embed the request_id in every Agent `description` field using the tag `[rid:<request_id>]` (e.g. `Harden Attacker round 1 [rid:harden-abc12345-1712345678-a1b2]`). The `post_tool_use.py` hook parses this tag from the agent description to attribute cost to this run. Do not omit the tag — without it, token spend will not be counted and budget tracking will silently return 0.

> **Nothing that mutates state runs in Step 0.** The round counter and the git worktree are created in **Step 1.5**, *after* the Step 1 confirmation gate — generating `request_id` above is pure string computation with no side effect, so it is fine here. This keeps the decline path in Step 1 honest: if the user does not choose Proceed, no counter file and no worktree branch are ever created.

---

## Step 1: Pre-run confirmation

Compute the estimated token cost for this run. Reuse `total_kb` from Step 0 (the size of all resolved target files after the pre-flight size check):

```
target_size_kb = total_kb   # already computed in Step 0
per_call = 15000 + 1.5 * target_size_kb * 256

# calls per round depends on the path:
#   legacy / single-lens : 1 attacker + 1 defender                 = 2
#   multi-lens (N lenses) : N attackers + 1 consolidator + 1 defender = N + 2
if path == "multi-lens":
    calls_per_round = len(resolved_lenses) + 2
else:
    calls_per_round = 2

estimated = int(rounds * calls_per_round * per_call)
```

The formula accounts for a 15 000-token base per agent call plus ~384 tokens per KB of target content per call. On the multi-lens path each of the N lens attackers reads the target independently (no cross-lens prompt-cache win is assumed — see spec C-2026-48), the consolidator adds one call, and the defender one more. (Base formula source: scribe note C-2026-24.)

Show the user what will happen:

```
**Harden configuration:**
- Mode: <code|plan>
- Path: <legacy | single-lens | multi-lens>
- Target: <file list or note #id>
- Lenses: <comma-separated resolved_lenses, or "—" on the legacy path>
- Focus: <focus type, legacy path only>
- Deep mode: <yes/no>
- Max rounds: <N>
- Attacker model: <model>
- Consolidator model: <model, multi-lens path only>
- Defender model: <model>
- Estimated cost: ~<estimated> tokens
- Token budget: <budget_tokens>
```

If `estimated > budget_tokens`, print this warning **before** calling AskUserQuestion:

```
WARNING: Estimated cost (<estimated>) exceeds budget (<budget_tokens>). Forcing through will hard-abort mid-run on overrun.
```

Use AskUserQuestion to confirm:
- **Proceed** → continue to Step 1.5 (create the round counter + worktree), then Step 2
- **Adjust** → user modifies settings, re-show confirmation

If the user declines (does not choose Proceed), stop immediately — do not start the round counter, do not create the worktree, do not spawn any Agent. Because both side effects live in Step 1.5 below, declining here leaves no state to clean up.

---

## Step 1.5: Commit to the run (post-confirmation side effects)

Only reached once the user chose **Proceed** in Step 1. Run these in order — the worktree-readiness check comes first so a bad target aborts before any state is created.

### Worktree-readiness check (code mode only)

The worktree is created from `HEAD`, so a target file that is untracked (or whose latest edits are uncommitted) will not exist — or will be stale — inside the worktree, and the Defender's worktree-path edits will fail or operate on the wrong content. For each resolved target file, check `git status --porcelain <file>`: if any is non-empty (untracked or uncommitted changes), abort with:

```
Target <file> has uncommitted changes or is untracked. Commit or stash it first — /harden operates on a worktree created from HEAD.
```

Do not start the round counter or create the worktree. (Plan mode has no worktree, so skip this check.)

### Start round counter

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py start --session-id <session_id>
```

### Create worktree (code mode only)

For code mode, create a single worktree that persists across all rounds. All Defenders will edit files here cumulatively, and Attackers in rounds 2+ will read from here to see the accumulated fixes.

```bash
git worktree add .claude/worktrees/harden-<session_id> -b harden-<session_id> HEAD
```

Save the worktree path (`.claude/worktrees/harden-<session_id>`) and branch name (`harden-<session_id>`) for use throughout the loop.

---

## Step 2: Attack-Defend loop

Resolve the apiary repo path, then read the agent prompt templates once before the loop:

```
apiary_repo = output of `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" --print-repo-path`
defender_template  = contents of <apiary_repo>/harden/agents/defender.md
# legacy/plan path:
attacker_template  = contents of <apiary_repo>/harden/agents/attacker.md
# lens path (single-lens or multi-lens):
lens_attacker_template = contents of <apiary_repo>/harden/agents/attacker_lens.md
consolidator_template  = contents of <apiary_repo>/harden/agents/consolidator.md   # multi-lens only
```

Initialize `prev_defender_output` to "None (first round)" and (lens path) `prior_record` to "None (first round)".

### Round routing

Each round produces a **validated findings set** and a matching **`expected_ids`** list, then hands them to the Defender. How the findings are produced depends on `path`:

- **`legacy`** (plan mode or code+`--focus`) → do **2a–2d** (single attacker, `ATK-NNN`), then continue at **2e**.
- **`single-lens`** → do **2L** with the one resolved lens; the lens attacker's `ATK-<CODE>-NNN` findings ARE the validated set (referee skipped). Continue at **2e**.
- **`multi-lens`** → do **2L** for all lenses in parallel, then **2C** (consolidator); the accepted `CON-NNN` set is the validated set. Continue at **2e**.

From **2e** onward (Defender invocation, spend, todos, budget, state) every path is identical — the Defender is agnostic to whether `expected_ids` are `ATK-NNN`, `ATK-<CODE>-NNN`, or `CON-NNN`.

---

### 2L. Lens attackers (single-lens and multi-lens paths)

#### 2L-a. Prepare each lens attacker prompt

For **each** lens in `resolved_lenses`, take `lens_attacker_template` and replace:
- `{{MODE}}` → `code`
- `{{LENS}}` → the lens name
- `{{LENS_BRIEF}}` → that lens's brief and `{{SEAM_RULES}}` → the shared seam rules. Read both once from `python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/lenses.py json` (keys `briefs`, `seam_rules`).
- `{{DEEP}}` → `true`/`false`
- `{{PREV_ROUND}}` → `prior_record`
- `{{TARGET_CONTENT}}` → instruct the attacker to read the files by path. **Rounds 2+: use the worktree paths** so attackers see cumulative Defender edits; round 1: original repo paths.

Append the same original-path instruction used elsewhere:
> "In your findings, always use the ORIGINAL relative file paths (e.g. `src/app.py:45-50`), not the worktree paths."

#### 2L-b. Spawn the lens attackers **in parallel**

Spawn one **foreground, read-only** Agent (subagent_type: "general-purpose") per lens, **all in a single message** so they run concurrently. For each:
- **description:** `Harden Attacker <lens> round <N> [rid:<request_id>]` (the `[rid:...]` tag attributes cost; the `<lens>` makes per-lens spend legible in budgeter logs)
- **model:** value of `--model-attacker`
- **prompt:** that lens's prepared prompt
- **Do NOT** pass `isolation` — attackers only Read; they never edit, so no worktree of their own.

Each must return ONLY a JSON array (no fences, no prose).

#### 2L-c. Validate + assign per lens

For each lens's output, strip any markdown fences, write to a temp file, then:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/validate_and_assign.py findings \
  --file <temp_file> --lens <lens> --sanitize --check-files [--deep]
```

This sanitizes (strips stray fields, injects the `lens`), validates in lens mode, and assigns `ATK-<CODE>-NNN`. Use `--deep` when the deep flag is set.

**Per-lens failure handling** (does not abort the round):
1. On validation failure, re-spawn just that lens's attacker once with the errors appended as feedback, then re-validate.
2. If the retry also fails, **drop that lens for this round**, record `"lens <name>: dropped (unparseable output)"` in the round summary, and continue with the remaining lenses.

Collect every lens's validated findings into one combined list `combined_findings` (each item carries its `id` = `ATK-<CODE>-NNN` and `lens`). Save the list of all assigned IDs as `source_ids`.

**If `combined_findings` is empty** (every lens found nothing or was dropped): treat exactly like the legacy **empty-findings** path in 2c — tick the round counter, report spend, and exit the loop to Step 3 (target clean).

#### Single-lens shortcut

If `path == single-lens`, **skip 2C entirely**. The lone lens's `combined_findings` is the validated findings set and `source_ids` is `expected_ids`. Continue at **2e**.

---

### 2C. Consolidator / referee (multi-lens path only)

#### 2C-a. Prepare the consolidator prompt

Take `consolidator_template` and replace:
- `{{MODE}}` → `code`
- `{{PRIOR_RECORD}}` → `prior_record`
- `{{FINDINGS_JSON}}` → `combined_findings` (the merged `ATK-<CODE>-NNN` list)

#### 2C-b. Spawn the consolidator

Spawn ONE **foreground, read-only** Agent (subagent_type: "general-purpose"):
- **description:** `Harden Consolidator round <N> [rid:<request_id>]`
- **model:** value of `--model-consolidator`
- **prompt:** the prepared consolidator prompt

It must return ONLY a JSON object `{ "accepted": [...], "rejected": [...] }`.

#### 2C-c. Validate + assign CON-NNN

Strip fences, write to a temp file, then:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/validate_and_assign.py consolidation \
  --file <temp_file> --source-ids <comma-joined source_ids> --check-files
```

This checks every dispatched `ATK-<CODE>-NNN` is accounted for exactly once (accepted-merged or rejected), validates the shape, and assigns `CON-NNN` to the accepted set.

**Failure handling (graceful degradation):**
1. On validation failure, re-spawn the consolidator once with the errors appended, then re-validate.
2. If the retry also fails, **degrade**: build the consolidated set deterministically instead of adjudicating —
   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/validate_and_assign.py consolidation \
     --degrade --file <combined_findings_temp_file>
   ```
   This dedups `combined_findings` by location (keeping the highest severity, collecting `source_ids`/`lenses`) and assigns `CON-NNN` with no rejections. Record `"consolidator: degraded (adjudication skipped)"` in the round summary.

Log the consolidator's `rejected` entries (id + reason) to the round summary either way.

The validated **accepted** set (`CON-NNN` items) is the findings set; the list of `CON-NNN` ids is `expected_ids`. Continue at **2e**.

---

### Legacy / plan findings production (path = legacy)

#### 2a. Prepare Attacker prompt

Take `attacker_template` and replace the placeholders:
- `{{MODE}}` → `code` or `plan`
- `{{FOCUS}}` → the focus type
- `{{DEEP}}` → `true` or `false`
- `{{PREV_DEFENDER}}` → `prev_defender_output`
- `{{TARGET_CONTENT}}` → For code mode: instruct the agent to read the files by path. **For rounds 2+, use the worktree paths** (e.g. `<worktree_path>/file.py`) so the Attacker sees the cumulative Defender edits. For round 1, use the original repo paths. For plan mode: paste the note content directly.

**Important:** Append this instruction for code mode:
> "In your findings, always use the ORIGINAL relative file paths (e.g. `src/app.py:45-50`), not the worktree paths. The location field must match the original project structure."

#### 2b. Spawn Attacker agent

Spawn a **foreground** Agent (subagent_type: "general-purpose") with:
- **description:** `Harden Attacker round <N> [rid:<request_id>]` (substitute round number and request_id — the `[rid:...]` tag is parsed by `post_tool_use.py` to attribute cost; the human-readable prefix becomes the agent_type in budgeter logs for per-round attribution)
- **model:** value of `--model-attacker`
- **prompt:** the prepared Attacker prompt

The agent must return ONLY a JSON array. Instruct it clearly:
> "Return ONLY a raw JSON array. No markdown fences, no explanation. Just the JSON."

#### 2c. Process Attacker output

Extract the JSON array from the agent's response. If the response contains markdown fences, strip them.

Write the extracted JSON to a temp file, then run validate-and-assign to sanitize, validate, and assign IDs in one step:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/validate_and_assign.py findings --file <temp_file> --sanitize [--check-files] [--deep]
```

Use `--check-files` in code mode. Use `--deep` if the deep flag is set. The `--sanitize` flag auto-strips unknown fields (e.g. `title`, `fix`) and maps invalid categories (e.g. `correctness` → `logic`) before validation.

**On validation failure:**
1. Show the error to the user briefly: "Attacker output validation failed: <errors>. Retrying..."
2. Re-spawn the Attacker with the validation errors appended to the prompt as feedback.
3. Run validate_and_assign.py again on the retry output.
4. If retry also fails: show the errors and use AskUserQuestion — "Continue to next round or stop?"
   - Continue → skip this round, proceed to next
   - Stop → jump to Step 3 with partial results

**On empty findings (`[]`):**

Tick the round counter first (so Step 4's `rounds=<N completed>/<N max>` includes this round):

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py tick --session-id <session_id>
```

Then query the running spend for this request (same as Step 2g). Note: because the Defender was not invoked on this path, spend covers only the Attacker — this is expected and will show lower than the per-round estimate:

```bash
spent=$(python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" budgeter/query_request.py --request-id <request_id> --cwd <session_cwd> 2>&1)
spent_status=$?
```

Check `spent_status` first, then validate the value is a plain integer:

- If `spent_status == 0` and `spent` matches `^[0-9]+$`: compute `pct = round(100 * spent / budget_tokens)` and show:
  ```
  Round N: Attacker found 0 issues. Code/plan looks clean. | spent <spent> of <budget_tokens> (<pct>%)
  ```
- Otherwise (non-zero exit or non-integer output): show:
  ```
  Round N: Attacker found 0 issues. Code/plan looks clean. | spent: unknown (<error trimmed to first 80 chars>)
  ```

Do not apply the BUDGET EXCEEDED marker on this path even if spent > budget — the empty-findings exit means the run completed cleanly.

- Exit the loop. Jump to Step 3.

#### 2d. Show Attacker summary

```
Round <N>: Attacker found <total> issues (<critical> critical, <high> high, <medium> medium, <low> low)
```

#### 2e. Invoke Defender agent

The Defender persists across rounds. It is spawned once in round 1 and continued via SendMessage in subsequent rounds. Its agent ID is stored externally in the round state file — never hold it in LLM context.

##### Round 1: Spawn the Defender

Take `defender_template` and replace:
- `{{MODE}}` → `code` or `plan`
- `{{FINDINGS_JSON}}` → the round's validated findings set. The `finding_ref` IDs the Defender must address are whatever this path produced: `ATK-NNN` (legacy), `ATK-<CODE>-NNN` (single-lens), or `CON-NNN` (multi-lens). The Defender is agnostic to the prefix.
- `{{TARGET_CONTENT}}` → For code mode: list the file paths **using the worktree paths** (e.g. `<worktree_path>/file.py`) so the Defender reads and edits the cumulative state. For plan mode: paste the note content directly.

**Important:** Append this instruction for code mode:
> "Read and edit files at the worktree paths provided. In your JSON response, use the ORIGINAL relative file paths (e.g. `src/app.py`) in the `changes.file` field, not the worktree paths."

Spawn a **foreground** Agent (subagent_type: "general-purpose") with:
- **description:** `Harden Defender round 1 [rid:<request_id>]` (the `[rid:...]` tag is parsed by `post_tool_use.py` to attribute cost; the human-readable prefix becomes the agent_type in budgeter logs; round 2+ continuations go through SendMessage and will not produce additional Agent tool entries)
- **model:** value of `--model-defender`
- **prompt:** the prepared Defender prompt
- **Do NOT use `isolation: "worktree"`** — the Defender edits the shared worktree directly

**Important for code mode:** Append this instruction to the prompt:

> "WORKFLOW: First, use the Read tool to read each target file. Then use the Edit tool to make your fixes — this is required, do not skip it. After all edits are complete, return your JSON summary. The JSON documents what you already changed, it is not a plan."

After the Defender responds, store its agent ID in the round state file:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py defender --session-id <session_id> --set <agent_id>
```

Where `<agent_id>` is the internal agent ID returned by the Agent tool (shown in the tool result).

##### Round 2+: Continue the existing Defender

Read the Defender agent ID from the state file:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py defender --session-id <session_id> --get
```

If this fails (exit code 1), abort the harden run: "Defender state corrupted — no agent ID found. Aborting."

Send the continuation message to the existing Defender via **SendMessage** using the retrieved agent ID. The continuation message is:

```
## Round <N> Findings

The reviewers re-examined your fixes and found <count> new issues.

### New findings
<this round's validated findings set — ATK-NNN, ATK-<CODE>-NNN, or CON-NNN per path>

### Previous round summary
- Fixed: <fixed_count> (<comma-separated fixed finding IDs>)
- Deferred: <deferred_count> (<comma-separated deferred finding IDs>)

Apply the same process: fix what you can in the worktree, defer what you can't, then return your JSON summary.
```

The "previous round summary" uses the mechanical counts and IDs from the previous round's validated Defender output — not LLM recall.

**If the Defender agent errors during continuation**, abort the harden run: "Defender agent failed on round N. Aborting." Do not attempt to respawn a fresh Defender.

**Note on token attribution for round 2+ continuations:** SendMessage continuations do not produce a new Agent tool entry, so their tokens land on the parent session bucket. The run still aborts on overrun via the parent-session fallback path; this is a documented v1 precision loss.

#### 2f. Process Defender output

Extract the JSON object from the agent's response. Strip markdown fences if present.

Collect the expected IDs from the round's findings set (the `id` field of each item the Defender was asked to address — `ATK-NNN`, `ATK-<CODE>-NNN`, or `CON-NNN` depending on path):

```
expected_ids = comma-separated list of all finding IDs from this round's validated findings set
```

Write the extracted JSON to a temp file, then run validate-and-assign to validate and assign IDs in one step:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/validate_and_assign.py response --file <temp_file> --expected-ids <expected_ids> [--check-files]
```

Use `--check-files` in code mode. validate_and_assign.py handles extracting the `responses` array, assigning DEF-IDs, and validating the full object.

**On validation failure:** same retry pattern as Attacker (one retry, then ask user).

#### 2g. Show round summary

Query the running spend for this request before printing the summary:

```bash
spent=$(python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" budgeter/query_request.py --request-id <request_id> --cwd <session_cwd> 2>&1)
spent_status=$?
```

Check `spent_status` first; only if it is 0 then verify `spent` matches `^[0-9]+$` (guards against exception messages that happen to start with digits being misclassified as valid token counts):

- If `spent_status == 0` and `spent` matches `^[0-9]+$`: compute `pct = round(100 * spent / budget_tokens)` and append the spend info to the summary line:

```
Round <N> summary: <fixed> fixed, <refactored> refactored, <deferred> deferred | spent <spent> of <budget_tokens> (<pct>%)
```

- If `spent_status != 0` (e.g. `usage_log.jsonl` missing or unreadable): append the error instead and **do not abort** — continue the loop:

```
Round <N> summary: <fixed> fixed, <refactored> refactored, <deferred> deferred | spent: unknown (<error trimmed to first 80 chars>)
```

Save `spent` (integer on success, `None` on error) to a variable for Step 2j to read.

#### 2h. Save Defender TODOs

For each item in the Defender's `todos` array:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type todo \
  --content "<todo content> (from /harden round <N>)" \
  --session-id "<session_id>" --auto
```

#### 2i. Save deferred findings as TODOs

For each response in the Defender's output where `action` is `deferred`, create a TODO note so the deferred item is individually trackable:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type todo \
  --content "Deferred <finding_ref>: <finding description> — Reason: <deferral reason from Defender> (from /harden round <N>)" \
  --session-id "<session_id>" --auto
```

The `<finding description>` comes from the original Attacker finding (matched by `finding_ref`). The `<deferral reason>` comes from the Defender's response `description` field.

#### 2j. Update state

- Set `prev_defender_output` to the validated Defender JSON (so the next round's Attacker sees what was fixed).
- **Lens path:** set `prior_record` to a compact summary the next round's lens attackers and consolidator use to avoid re-surfacing handled work — for each finding this round: its ID, location, and outcome (`fixed` / `refactored` / `deferred` from the Defender, plus any `CON` rejections with reasons from 2C). This is mechanical (built from validated output), not LLM recall.
- Tick the round counter:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py tick --session-id <session_id>
```

#### 2k. Budget abort check

If the `spent` value captured in Step 2g is a known integer (not `None`) and `spent > budget_tokens`:

- Set a run-level flag `budget_exceeded = True` (this flag is consumed by Step 4 — preserve it across the early-exit jump).
- Print:
  ```
  BUDGET EXCEEDED: spent <spent> > budget <budget_tokens>. Aborting after round <N> with partial results.
  ```
- Skip all remaining rounds and jump directly to Step 3. Do **not** reset the worktree — the `harden-<session_id>` branch must be preserved so the user can inspect partial work.

If `spent` is `None` (helper errored), do **not** abort — continue the loop normally.

#### 2l. Early exit check

If ALL findings in this round were deferred (no fixes or refactors), and this is not the first round, consider exiting early. Show: "All findings deferred — no further fixes possible. Exiting loop."

Note: if `budget_exceeded` fired in Step 2k, control has already jumped to Step 3 — this step is unreachable on a budget-exceeded round.

---

## Step 3: Present results

### Code mode

Show the accumulated diff from the shared worktree:

1. Show the diff:
   ```bash
   git -C <worktree_path> diff HEAD
   ```
   If the diff is empty (no changes were made across all rounds), warn: "Defenders did not make any file edits. No code changes to review."
2. Show summary:
   ```
   **Harden complete.** <N> rounds, <total_findings> findings, <total_fixed> fixed, <total_deferred> deferred.
   Branch: `<worktree_branch>`
   ```
3. Use AskUserQuestion:
   - **Approve** → proceed to Step 4
   - **Discard** → run `git worktree remove <worktree_path>`, save summary note anyway, stop

### Plan mode

Show the amended spec from the last Defender's `amended_spec` field. Use AskUserQuestion:
- **Approve** → proceed to Step 4
- **Discard** → save summary note anyway, stop

### No findings

If round 1 produced no findings (the single Attacker, or every lens attacker, found nothing):

```
**Harden complete.** No issues found — the target looks solid.
```

Save a brief summary note and stop.

---

## Step 4: Save

### Build summary

Compile a summary of all rounds:

```
## /harden Summary

**BUDGET EXCEEDED**
Spend at abort: <spent> of <budget_tokens> tokens

**Target:** <files or note #id>
**Settings:** path=<legacy|single-lens|multi-lens>, lenses=<resolved_lenses or —>, focus=<focus, legacy only>, deep=<yes/no>, rounds=<N completed>/<N max>
**Models:** attacker=<model>, consolidator=<model, multi-lens only>, defender=<model>
```

The two lines `**BUDGET EXCEEDED**` and `Spend at abort: ...` are the very first lines of the summary body and are included only when `budget_exceeded == True`. When `budget_exceeded` is `False`, omit them entirely and start directly with `**Target:**`:

```
## /harden Summary

**Target:** <files or note #id>
**Settings:** path=<legacy|single-lens|multi-lens>, lenses=<resolved_lenses or —>, focus=<focus, legacy only>, deep=<yes/no>, rounds=<N completed>/<N max>
**Models:** attacker=<model>, consolidator=<model, multi-lens only>, defender=<model>

### Round-by-round

#### Round 1
**Findings:** <count>
- <finding-id> [severity] description → DEF-001 [action] description
- <finding-id> [severity] description → DEF-002 [action] description
```

On the **multi-lens** path, show the full trace per accepted finding so the lens→referee→defender chain is auditable, and list referee rejections:

```
#### Round 1
**Lenses run:** <comma-separated, with any dropped lenses noted>
**Findings:** <accepted count> accepted (<rejected count> rejected by referee)
- CON-001 [severity] description  ⟵ ATK-SEC-001, ATK-COR-003 (security, correctness)  → DEF-001 [fixed] description
- CON-002 [severity] description  ⟵ ATK-RES-002 (resilience)                          → DEF-002 [deferred] description

**Referee rejections:**
- ATK-CPX-004 — Reason: <why rejected>
```

Then close with:

```
### Deferred items
- <finding-id>: <description> — Reason: <why deferred>

### TODOs created
- <todo content>
```

### Save summary note

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py add --type context \
  --content "<summary>" \
  --session-id "<session_id>"
```

### Reset round counter

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" harden/round_counter.py reset --session-id <session_id>
```

### Final message

Code mode: "Summary saved. Worktree branch: `<branch_name>` — merge when ready."

Plan mode: "Summary saved. Amended spec is in the output above — use it for implementation."
