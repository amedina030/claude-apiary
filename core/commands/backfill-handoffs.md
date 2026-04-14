---
name: backfill-handoffs
description: Process unseen session transcripts into handoff notes
user-invocable: true
---

Process unseen session transcripts listed in the `[startup]` context block. Only run this if the `[startup]` context contains `unseen_sessions` that are NOT "none".

## What to do

### Step 0 — capture the pre-launch handoff count (for post-validation)

Before spawning the agent, run (from the apiary repo root):

```bash
find .apiary/scribe/handoffs -name index.jsonl -exec cat {} + 2>/dev/null | wc -l > /tmp/backfill_pre_count.txt
```

This counts every handoff record across both the year-folder v2 layout and the legacy flat layout (active + archived). You'll compare against it after the agent finishes (see "Post-condition validator" below).

### Step 1 — spawn the agent

Spawn an agent with `subagent_type: "general-purpose"`, `run_in_background: true`, `model: "sonnet"`.

**Do not use haiku** — it has been observed to fabricate final summaries (claiming handoffs written when none were) when it encounters tool-output size errors it can't recover from. Sonnet handles the error-recovery paths reliably.

Replace `<session_id>` with the current session ID from the `[session]` context (first 8 chars). If `[session]` context is not available, check `[budgeter]` context as a fallback.

---

**Agent prompt:**

You are a transcript processing agent. Your job is to generate handoffs and extract missed learnings/TODOs from unseen session transcripts.

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

For each unseen session from the `[startup]` context:

1. The context block lists each session as `<prefix> <transcript_path>` on its own line directly under the `unseen_sessions:` header. Use that path verbatim — do not glob. If no path is present (older context format), fall back to globbing under `C:/Users/amedi/.claude/projects/`.
2. Run `extract_transcript.py --summary <path>` to get the overview. This now exits non-zero on a missing path — if that happens, report the session under "failed" (bad path), do not silently treat as empty.
3. If `total_messages < 5` or all sampled messages are the startup context-injection, treat as startup-only and skip (but **still report it as skipped in the final summary, not as "generated"**).
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
**Extracted from transcripts:** <count> learnings, <count> TODOs — or "None"
```

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
