---
name: backfill-handoffs
description: Process unseen session transcripts into handoff notes
user-invocable: true
---

Process unseen session transcripts that don't yet have a handoff note. As of T-2026-164 the unseen list is no longer injected into `[startup]` context — fetch it explicitly via the CLI below. If the list is empty, tell the user there's nothing to back-fill and stop.

## What to do

### Step 0a — fetch the unseen-session list

Run this from the apiary repo root, passing the CURRENT session's full UUID (from `[session]` context, or `[budgeter]` as fallback) so it isn't counted against itself:

```bash
python ~/.claude/apiary_launch.py core/startup.py unseen --session-id <current-session-id> --repo-dir "$(pwd)"
```

Output is JSON: `{"count": N, "sessions": [{"session_id": "...", "transcript_path": "...", "role": "...", "mission": "..."}, ...]}`. If `count == 0`, stop — nothing to do. Otherwise, the `sessions` array drives the agent loop in Step 1.

### Step 0b — capture the pre-launch handoff count (for post-validation)

```bash
find .apiary/scribe/handoffs -name index.jsonl -exec cat {} + 2>/dev/null | wc -l > /tmp/backfill_pre_count.txt
```

This counts every handoff record across both the year-folder v2 layout and the legacy flat layout (active + archived). You'll compare against it after the agent finishes (see "Post-condition validator" below).

### Step 1 — spawn the agent

Spawn an agent with `subagent_type: "general-purpose"`, `run_in_background: true`, `model: "sonnet"`.

**Do not use haiku** — it has been observed to fabricate final summaries (claiming handoffs written when none were) when it encounters tool-output size errors it can't recover from. Sonnet handles the error-recovery paths reliably.

Include the full JSON list from Step 0a in the agent prompt (see "Unseen sessions" placeholder below) — the agent will not re-query the CLI. Replace `<session_id>` with the current session ID from the `[session]` context (first 8 chars). If `[session]` context is not available, check `[budgeter]` context as a fallback.

---

**Agent prompt:**

You are a transcript processing agent. Your job is to generate handoffs and extract missed learnings/TODOs from unseen session transcripts.

### Unseen sessions (paste JSON here)

The invoking session already ran `core/startup.py unseen` and captured the result. Paste the full JSON below when spawning. Do NOT re-query the CLI — the invoker already excluded its own session id.

```json
<paste {"count": N, "sessions": [...]} here>
```

Each entry in `sessions` has `session_id`, `transcript_path`, `role`, `mission`. Iterate over that list for Step 1.

### Tool-output size constraints (READ THIS FIRST)

- `extract_transcript.py` can produce 1KB–200KB+ of output. The Bash tool auto-persists large outputs to a file; the `Read` tool then rejects files over ~10k tokens (~40KB).
- **You have escape hatches — use them. Do not silently skip sessions because a file is too big.**

Safe pattern for each transcript:

1. Always start with `--summary` to get stats + 6 sampled messages. This is tiny and always fits.
   ```bash
   python ~/.claude/apiary_launch.py core/hooks/extract_transcript.py <path> --summary
   ```
2. If the summary suggests the session is real work (not startup-only), pull the body in chunks:
   ```bash
   python ~/.claude/apiary_launch.py core/hooks/extract_transcript.py <path> --head 30 --max-chars 1500
   python ~/.claude/apiary_launch.py core/hooks/extract_transcript.py <path> --tail 30 --max-chars 1500
   ```
3. If you need specific content from the middle, use grep/head/tail on the persisted file (paths like `tool-results/<id>.txt`) or re-run `extract_transcript.py` with tighter `--head`/`--max-chars`.
4. If `Read` errors with "exceeds maximum allowed tokens", immediately switch to `offset`/`limit` parameters or back to `extract_transcript.py` flags. **Never move on without actually ingesting the content.**

### Path portability

Transcript paths may contain Windows backslashes (e.g. `C:\Users\...\uuid.jsonl`). **Always convert backslashes to forward slashes** before using any path in a bash command.

### Step 1: Process unseen sessions

Iterate over the `sessions` array in the JSON data block the invoker pasted above (under "Unseen sessions"):

1. Each entry provides `session_id` and `transcript_path` verbatim — use that path as-is (do not glob). Convert backslashes to forward slashes before passing to bash.
2. Run `extract_transcript.py --summary <path>` to get the overview. This now exits non-zero on a missing path — if that happens, report the session under "failed" (bad path) AND add it to the skip list (see step 3a) with reason `"failed: bad path"` so it does not re-appear on every future startup.
3. If `total_messages < 5` or all sampled messages are the startup context-injection, treat as startup-only and skip (but **still report it as skipped in the final summary, not as "generated"**).
3a. **After a skip decision (startup-only or unrecoverable-failure), durably record it** so the session is never re-surfaced as unseen on future startups:
    ```bash
    python ~/.claude/apiary_launch.py core/startup.py skip \
        --session-id <full-or-8char-id> \
        --reason "startup-only"   # or "failed: <short reason>"
    ```
    The command is idempotent (no duplicate entries). Verify exit code 0 — if nonzero, report in the final summary's "failed" bucket and do NOT count it under "added to skip list".
4. Otherwise pull chunks via `--head` / `--tail` / `--max-chars` as needed until you have enough to write a handoff.
5. Produce a handoff body in this shape:
   ```
   ## Session <prev-id> Handoff
   **Role:** <role> | **Mission:** <mission>
   ### What was done
   ### Key decisions
   ### What's pending
   ### Where it stopped
   ```
   Be concise but specific — file names, function names, concrete details.
6. Save the handoff using a Python one-liner with list-form subprocess (avoids shell quoting breakage on Windows):
   ```bash
   python -c "import subprocess,os,sys; r=subprocess.run(['python', os.path.expanduser('~/.claude/apiary_launch.py'), 'scribe/notes.py', 'add', '--type', 'handoff', '--session-id', '<prev-id>', '--auto', '--if-no-handoff-for', '<prev-id>', '--summary', '<one-line abstract>', '--content', '''<handoff text>''']); sys.exit(r.returncode)"
   ```
   **`--summary` is required** — a single concrete sentence (≤300 chars) naming the area touched and the outcome. Do not restate "Session X handoff"; the session ID is already in the index.

7. **Verify the write succeeded.** Check the subprocess exit code. If nonzero, capture stderr and either retry with a different shell-quoting approach or report the failure in your final summary. **Do not count a failed write as a generated handoff.**

8. **Extract missed learnings and TODOs.** Look for:
   - Non-obvious workarounds or fixes that were discovered but not saved as learnings
   - Work deferred or left incomplete that wasn't captured as a TODO
   - Bugs observed but not tracked
   
   Save each via the same list-form subprocess pattern, and again verify exit codes.

### Step 2: Return summary

Return a message with EXACTLY this structure (no extras):

```
**Handoffs generated:** <count> — <list of session IDs, or "None">
**Sessions skipped (startup-only or empty):** <count> — <list, or "None">
**Sessions failed (ingestion/write error):** <count> — <list with short reason, or "None">
**Added to skip list:** <count> — <list of session IDs, or "None">
**Extracted from transcripts:** <count> learnings, <count> TODOs — or "None"
```

The "Added to skip list" count should equal `(startup-only skipped) + (failed sessions that were successfully skip-recorded)`. A session counted under "skipped" or "failed" but NOT under "Added to skip list" means the `startup.py skip` call failed — flag that explicitly.

**Accuracy rules** (hard requirements, not suggestions):
- The counts MUST match what actually happened. If you ran into a blocker and couldn't write a handoff, that session goes in "failed", not "generated".
- Do NOT invent "bug fixes pushed", "code changes", or "commits" — you were not asked to modify code.
- If you couldn't ingest ANY transcript content, all sessions go in "failed" and "generated" is 0. Fabricating success is worse than reporting 0.

Keep the entire output under 250 words.

---

## After launching the agent

Do NOT wait for the agent to finish. Immediately continue with the user's request. When the agent completes in the background, you MUST do two things in this order:

### 1. Post-condition validator (CRITICAL — catches agent hallucinations)

Re-count the handoff records and diff against the agent's claim:

```bash
find .apiary/scribe/handoffs -name index.jsonl -exec cat {} + 2>/dev/null | wc -l > /tmp/backfill_post_count.txt
pre=$(cat /tmp/backfill_pre_count.txt)
post=$(cat /tmp/backfill_post_count.txt)
delta=$((post - pre))
echo "Pre=$pre  Post=$post  Delta=$delta (agent claimed: <N>)"
```

- If `delta` matches the agent's claimed "Handoffs generated" count → everything is fine, output the agent's summary prefixed with "Transcripts processed:".
- If `delta < claimed` → the agent hallucinated. Output a warning to the user: "⚠️ Backfill agent reported N handoffs but only `delta` were actually written. The summary may be unreliable — recommend re-running with `--force` or investigating." Include both the agent's claim and the real delta.
- If `delta > claimed` → unexpected (duplicates?); still flag it.

Don't skip the validator even when you trust the agent. Haiku/sonnet both occasionally fabricate under tool-error stress, and the check is cheap.

### 2. Cost logging

The task notification includes a `<usage>` block. Pipe it to the logger:

```bash
echo '<usage>...</usage>' | python ~/.claude/apiary_launch.py budgeter/log_agent_cost.py --session-id "<full_session_id>" --agent "backfill-handoffs"
```

Use the full session UUID (not the 8-char prefix). Run this silently — do not mention cost logging to the user.
