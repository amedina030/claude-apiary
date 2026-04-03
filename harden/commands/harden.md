---
name: harden
description: Adversarial attack-defend loop that stress-tests code or plans
user-invocable: true
---

# /harden — Adversarial Code Hardening

Run an automated attack-defend loop where an Attacker agent finds weaknesses and a Defender agent fixes them. Works on code files or plan specs.

## Arguments

- `/harden file1.py [file2.py ...]` — harden one or more code files (max 5 by default)
- `/harden --plan <note-id>` — harden a scribe plan/spec note
- `/harden cancel` — cancel the current harden loop and exit

### Optional flags

- `--focus <type>` — focus area: `general` (default), `security`, `input`, `logic`, `complexity`, `resilience`
- `--deep` — require concrete Given/When/Then attack scenarios
- `--rounds N` — max rounds (default 3)
- `--max-files N` — max files allowed (default 5)
- `--model-attacker <model>` — model for Attacker agent (default `sonnet`)
- `--model-defender <model>` — model for Defender agent (default `sonnet`)

---

## Step 0: Parse and validate

### Cancel

If the argument is `cancel`:
1. Run: `python <repo_dir>/harden/round_counter.py reset --session-id <session_id>`
2. Respond: "Harden cancelled. No changes were made."
3. Stop.

### Parse arguments

Determine the mode from the arguments:

- If `--plan <note-id>` is present → **plan mode**. The note ID is the argument.
- Otherwise → **code mode**. All non-flag arguments are file paths.

Extract optional flags with their defaults:
- `--focus`: default `general`
- `--deep`: default `false`
- `--rounds`: default `3`
- `--max-files`: default `5`
- `--model-attacker`: default `sonnet`
- `--model-defender`: default `sonnet`

### Validate inputs

**Code mode:**
1. Check that at least one file path was provided. If not, tell the user and stop.
2. Check that the number of files does not exceed `--max-files`. If it does, abort with: "Too many files (N > max). Narrow scope or use `--max-files N`."
3. For each file, verify it exists using the Read tool. If any file is missing, list the missing files and stop.

**Plan mode:**
1. Run: `python <repo_dir>/scribe/notes.py get <note-id>`
2. If the note doesn't exist, tell the user: "Note <id> not found. Use `python scribe/notes.py list` to find the correct ID." Stop.
3. Save the note content for use in later steps.

### Start round counter

```bash
python <repo_dir>/harden/round_counter.py start --session-id <session_id>
```

---

## Step 1: Pre-run confirmation

Show the user what will happen:

```
**Harden configuration:**
- Mode: <code|plan>
- Target: <file list or note #id>
- Focus: <focus type>
- Deep mode: <yes/no>
- Max rounds: <N>
- Attacker model: <model>
- Defender model: <model>
```

Use AskUserQuestion to confirm:
- **Proceed** → continue to Step 2
- **Adjust** → user modifies settings, re-show confirmation

---

## Step 2: Attack-Defend loop

Read the agent prompt templates once before the loop:

```
attacker_template = contents of <repo_dir>/harden/agents/attacker.md
defender_template = contents of <repo_dir>/harden/agents/defender.md
```

Initialize `prev_defender_output` to "None (first round)".

### For each round (1 to max rounds):

#### 2a. Prepare Attacker prompt

Take `attacker_template` and replace the placeholders:
- `{{MODE}}` → `code` or `plan`
- `{{FOCUS}}` → the focus type
- `{{DEEP}}` → `true` or `false`
- `{{PREV_DEFENDER}}` → `prev_defender_output`
- `{{TARGET_CONTENT}}` → For code mode: instruct the agent to read the files by path. For plan mode: paste the note content directly.

#### 2b. Spawn Attacker agent

Spawn a **foreground** Agent (subagent_type: "general-purpose") with:
- **model:** value of `--model-attacker`
- **prompt:** the prepared Attacker prompt

The agent must return ONLY a JSON array. Instruct it clearly:
> "Return ONLY a raw JSON array. No markdown fences, no explanation. Just the JSON."

#### 2c. Process Attacker output

Extract the JSON array from the agent's response. If the response contains markdown fences, strip them.

**Validate first, then assign IDs** (the validator rejects the `id` field, so it must run on raw Attacker output):

```bash
echo '<attacker_json>' | python <repo_dir>/harden/validate_findings.py [--check-files] [--deep]
```

Use `--check-files` in code mode. Use `--deep` if the deep flag is set.

Then assign IDs to the validated output:

```bash
echo '<validated_json>' | python <repo_dir>/harden/assign_ids.py --prefix ATK
```

**On validation failure:**
1. Show the error to the user briefly: "Attacker output validation failed: <errors>. Retrying..."
2. Re-spawn the Attacker with the validation errors appended to the prompt as feedback.
3. Run assign_ids + validation again on the retry output.
4. If retry also fails: show the errors and use AskUserQuestion — "Continue to next round or stop?"
   - Continue → skip this round, proceed to next
   - Stop → jump to Step 3 with partial results

**On empty findings (`[]`):**
- Show: "Round N: Attacker found 0 issues. Code/plan looks clean."
- Exit the loop. Jump to Step 3.

#### 2d. Show Attacker summary

```
Round <N>: Attacker found <total> issues (<critical> critical, <high> high, <medium> medium, <low> low)
```

#### 2e. Spawn Defender agent

Take `defender_template` and replace:
- `{{MODE}}` → `code` or `plan`
- `{{FINDINGS_JSON}}` → the validated findings JSON (with ATK-NNN IDs)
- `{{TARGET_CONTENT}}` → For code mode: list the file paths for the agent to read and edit. For plan mode: paste the note content directly.

Spawn a **foreground** Agent (subagent_type: "general-purpose") with:
- **model:** value of `--model-defender`
- **prompt:** the prepared Defender prompt
- **For code mode only:** add `isolation: "worktree"` so the Defender edits files in an isolated worktree

**Important for code mode:** Append this instruction to the prompt:

> "WORKFLOW: First, use the Read tool to read each target file. Then use the Edit tool to make your fixes — this is required, do not skip it. After all edits are complete, return your JSON summary. The JSON documents what you already changed, it is not a plan."

The agent result will include worktree info (path and branch name) if files were changed. Save the `worktree_branch` and `worktree_path` for use in Step 3.

#### 2f. Process Defender output

Extract the JSON object from the agent's response. Strip markdown fences if present.

Collect the expected ATK-IDs from the findings:

```
expected_ids = comma-separated list of all ATK-NNN IDs from the findings
```

Pipe through ID assigner and validator:

```bash
echo '<defender_json>' | python <repo_dir>/harden/assign_ids.py --prefix DEF
```

Wait — the assign_ids script works on arrays, but the Defender output is an object with a `responses` array. So instead, extract the `responses` array, pipe it through assign_ids, then reassemble the object. Or better: pipe just the responses array:

```bash
echo '<responses_array>' | python <repo_dir>/harden/assign_ids.py --prefix DEF
```

Then reconstruct the full object with the ID-assigned responses, and validate:

```bash
echo '<full_defender_json>' | python <repo_dir>/harden/validate_response.py --expected-ids <expected_ids> [--check-files]
```

Use `--check-files` in code mode.

**On validation failure:** same retry pattern as Attacker (one retry, then ask user).

#### 2g. Show round summary

```
Round <N> summary: <fixed> fixed, <refactored> refactored, <deferred> deferred
```

#### 2h. Save Defender TODOs

For each item in the Defender's `todos` array:

```bash
python <repo_dir>/scribe/notes.py add --type todo \
  --content "<todo content> (from /harden round <N>)" \
  --session-id "<session_id>" --auto
```

#### 2i. Update state

- Set `prev_defender_output` to the validated Defender JSON (so the next round's Attacker sees what was fixed).
- Tick the round counter:

```bash
python <repo_dir>/harden/round_counter.py tick --session-id <session_id>
```

#### 2j. Early exit check

If ALL findings in this round were deferred (no fixes or refactors), and this is not the first round, consider exiting early. Show: "All findings deferred — no further fixes possible. Exiting loop."

---

## Step 3: Present results

### Code mode

If any Defender agent ran with worktree isolation and returned a `worktree_branch` and `worktree_path`:

1. Show the diff from the worktree:
   ```bash
   git -C <worktree_path> diff HEAD
   ```
2. Show summary:
   ```
   **Harden complete.** <N> rounds, <total_findings> findings, <total_fixed> fixed, <total_deferred> deferred.
   Branch: `<worktree_branch>`
   ```
3. Use AskUserQuestion:
   - **Approve** → proceed to Step 4
   - **Discard** → run `git worktree remove <worktree_path>`, save summary note anyway, stop

If no worktree was returned (Defender didn't edit files), warn the user:
> "Defender did not make any file edits. No code changes to review."

### Plan mode

Show the amended spec from the last Defender's `amended_spec` field. Use AskUserQuestion:
- **Approve** → proceed to Step 4
- **Discard** → save summary note anyway, stop

### No findings

If the Attacker found nothing in round 1:

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

**Target:** <files or note #id>
**Settings:** focus=<focus>, deep=<yes/no>, rounds=<N completed>/<N max>
**Models:** attacker=<model>, defender=<model>

### Round-by-round

#### Round 1
**Findings:** <count>
- ATK-001 [severity] description → DEF-001 [action] description
- ATK-002 [severity] description → DEF-002 [action] description

#### Round 2
...

### Deferred items
- ATK-003: <description> — Reason: <why deferred>

### TODOs created
- <todo content>
```

### Save summary note

```bash
python <repo_dir>/scribe/notes.py add --type context \
  --content "<summary>" \
  --session-id "<session_id>"
```

### Reset round counter

```bash
python <repo_dir>/harden/round_counter.py reset --session-id <session_id>
```

### Final message

Code mode: "Summary saved. Worktree branch: `<branch_name>` — merge when ready."

Plan mode: "Summary saved. Amended spec is in the output above — use it for implementation."
