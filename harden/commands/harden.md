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
- Otherwise → **code mode**. All non-flag arguments are file paths or directory paths.

### Expand directories

After extracting the non-flag arguments, check each one: if it's a directory (use Bash `test -d` or equivalent), expand it into individual files using the Glob tool with the pattern `**/*.{py,js,ts,tsx,jsx,go,rs,java,rb,sh}` rooted at that directory. Replace the directory argument with the expanded file list.

**Exclusions:** Skip files matching these patterns:
- `__pycache__/`, `node_modules/`, `.git/`
- `test_*.py`, `*_test.py`, `*_test.go`, `*.test.ts`, `*.test.js`, `*.spec.ts`, `*.spec.js`

After expansion, deduplicate the full file list (in case a file was listed both explicitly and via directory).

Extract optional flags with their defaults:
- `--focus`: default `general`
- `--deep`: default `false`
- `--rounds`: default `3`
- `--max-files`: default `5`
- `--model-attacker`: default `sonnet`
- `--model-defender`: default `sonnet`

### Validate inputs

**Code mode:**
1. Check that at least one file path was provided (after directory expansion). If not, tell the user and stop. If a directory was provided but expansion found 0 matching files, tell the user: "No code files found in `<dir>`. Check the path or add files explicitly."
2. Check that the number of files does not exceed `--max-files`. If it does, abort with: "Too many files (N > max). Narrow scope, use `--max-files N`, or pass specific files instead of a directory."
3. For each file, verify it exists using the Read tool. If any file is missing, list the missing files and stop.

**Plan mode:**
1. Run: `python <repo_dir>/scribe/notes.py get <note-id>`
2. If the note doesn't exist, tell the user: "Note <id> not found. Use `python scribe/notes.py list` to find the correct ID." Stop.
3. Save the note content for use in later steps.

### Start round counter

```bash
python <repo_dir>/harden/round_counter.py start --session-id <session_id>
```

### Create worktree (code mode only)

For code mode, create a single worktree that persists across all rounds. All Defenders will edit files here cumulatively, and Attackers in rounds 2+ will read from here to see the accumulated fixes.

```bash
git worktree add .claude/worktrees/harden-<session_id> -b harden-<session_id> HEAD
```

Save the worktree path (`.claude/worktrees/harden-<session_id>`) and branch name (`harden-<session_id>`) for use throughout the loop.

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
- `{{TARGET_CONTENT}}` → For code mode: instruct the agent to read the files by path. **For rounds 2+, use the worktree paths** (e.g. `<worktree_path>/file.py`) so the Attacker sees the cumulative Defender edits. For round 1, use the original repo paths. For plan mode: paste the note content directly.

**Important:** Append this instruction for code mode:
> "In your findings, always use the ORIGINAL relative file paths (e.g. `src/app.py:45-50`), not the worktree paths. The location field must match the original project structure."

#### 2b. Spawn Attacker agent

Spawn a **foreground** Agent (subagent_type: "general-purpose") with:
- **description:** `Harden Attacker round <N>` (substitute the current round number — this becomes the agent_type in budgeter logs, giving per-round attribution)
- **model:** value of `--model-attacker`
- **prompt:** the prepared Attacker prompt

The agent must return ONLY a JSON array. Instruct it clearly:
> "Return ONLY a raw JSON array. No markdown fences, no explanation. Just the JSON."

#### 2c. Process Attacker output

Extract the JSON array from the agent's response. If the response contains markdown fences, strip them.

Write the extracted JSON to a temp file, then run the pipeline to sanitize, validate, and assign IDs in one step:

```bash
python <repo_dir>/harden/pipeline.py findings --file <temp_file> --sanitize [--check-files] [--deep]
```

Use `--check-files` in code mode. Use `--deep` if the deep flag is set. The `--sanitize` flag auto-strips unknown fields (e.g. `title`, `fix`) and maps invalid categories (e.g. `correctness` → `logic`) before validation.

**On validation failure:**
1. Show the error to the user briefly: "Attacker output validation failed: <errors>. Retrying..."
2. Re-spawn the Attacker with the validation errors appended to the prompt as feedback.
3. Run the pipeline again on the retry output.
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

#### 2e. Invoke Defender agent

The Defender persists across rounds. It is spawned once in round 1 and continued via SendMessage in subsequent rounds. Its agent ID is stored externally in the round state file — never hold it in LLM context.

##### Round 1: Spawn the Defender

Take `defender_template` and replace:
- `{{MODE}}` → `code` or `plan`
- `{{FINDINGS_JSON}}` → the validated findings JSON (with ATK-NNN IDs)
- `{{TARGET_CONTENT}}` → For code mode: list the file paths **using the worktree paths** (e.g. `<worktree_path>/file.py`) so the Defender reads and edits the cumulative state. For plan mode: paste the note content directly.

**Important:** Append this instruction for code mode:
> "Read and edit files at the worktree paths provided. In your JSON response, use the ORIGINAL relative file paths (e.g. `src/app.py`) in the `changes.file` field, not the worktree paths."

Spawn a **foreground** Agent (subagent_type: "general-purpose") with:
- **description:** `Harden Defender round 1` (becomes the agent_type in budgeter logs; round 2+ continuations go through SendMessage and will not produce additional Agent tool entries)
- **model:** value of `--model-defender`
- **prompt:** the prepared Defender prompt
- **Do NOT use `isolation: "worktree"`** — the Defender edits the shared worktree directly

**Important for code mode:** Append this instruction to the prompt:

> "WORKFLOW: First, use the Read tool to read each target file. Then use the Edit tool to make your fixes — this is required, do not skip it. After all edits are complete, return your JSON summary. The JSON documents what you already changed, it is not a plan."

After the Defender responds, store its agent ID in the round state file:

```bash
python <repo_dir>/harden/round_counter.py defender --session-id <session_id> --set <agent_id>
```

Where `<agent_id>` is the internal agent ID returned by the Agent tool (shown in the tool result).

##### Round 2+: Continue the existing Defender

Read the Defender agent ID from the state file:

```bash
python <repo_dir>/harden/round_counter.py defender --session-id <session_id> --get
```

If this fails (exit code 1), abort the harden run: "Defender state corrupted — no agent ID found. Aborting."

Send the continuation message to the existing Defender via **SendMessage** using the retrieved agent ID. The continuation message is:

```
## Round <N> Findings

The Attacker re-examined your fixes and found <count> new issues.

### New findings
<validated findings JSON with ATK-NNN IDs>

### Previous round summary
- Fixed: <fixed_count> (<comma-separated fixed ATK-IDs>)
- Deferred: <deferred_count> (<comma-separated deferred ATK-IDs>)

Apply the same process: fix what you can in the worktree, defer what you can't, then return your JSON summary.
```

The "previous round summary" uses the mechanical counts and IDs from the previous round's validated Defender output — not LLM recall.

**If the Defender agent errors during continuation**, abort the harden run: "Defender agent failed on round N. Aborting." Do not attempt to respawn a fresh Defender.

#### 2f. Process Defender output

Extract the JSON object from the agent's response. Strip markdown fences if present.

Collect the expected ATK-IDs from the findings:

```
expected_ids = comma-separated list of all ATK-NNN IDs from the findings
```

Write the extracted JSON to a temp file, then run the pipeline to validate and assign IDs in one step:

```bash
python <repo_dir>/harden/pipeline.py response --file <temp_file> --expected-ids <expected_ids> [--check-files]
```

Use `--check-files` in code mode. The pipeline handles extracting the `responses` array, assigning DEF-IDs, and validating the full object.

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

#### 2i. Save deferred findings as TODOs

For each response in the Defender's output where `action` is `deferred`, create a TODO note so the deferred item is individually trackable:

```bash
python <repo_dir>/scribe/notes.py add --type todo \
  --content "Deferred <finding_ref>: <finding description> — Reason: <deferral reason from Defender> (from /harden round <N>)" \
  --session-id "<session_id>" --auto
```

The `<finding description>` comes from the original Attacker finding (matched by `finding_ref`). The `<deferral reason>` comes from the Defender's response `description` field.

#### 2j. Update state

- Set `prev_defender_output` to the validated Defender JSON (so the next round's Attacker sees what was fixed).
- Tick the round counter:

```bash
python <repo_dir>/harden/round_counter.py tick --session-id <session_id>
```

#### 2k. Early exit check

If ALL findings in this round were deferred (no fixes or refactors), and this is not the first round, consider exiting early. Show: "All findings deferred — no further fixes possible. Exiting loop."

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
