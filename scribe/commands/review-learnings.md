---
name: review-learnings
description: Walk through all learnings grouped by tag, archive or supersede stale entries, stamp last_review timestamp
user-invocable: true
---

Walk the user through the project's learning corpus, one tag group at a time, so stale or duplicate entries can be archived or superseded. Stamps `last_review` when done so the startup banner stops nudging.

## Steps

1. Resolve the scribe state dir:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py learnings --index
   ```

   If the command fails (not inside a git repo, etc.), tell the user and stop.

2. Present the tag-grouped index. Tell the user you'll walk through each tag group and ask what to do with each learning.

3. For each tag group (in order from the `--index` output):

   a. List the learnings in that group — show ID, brief summary (use the output from step 1), and for any entry the user wants details on, fetch full body via:

      ```bash
      python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py get <ID>
      ```

   b. Ask, in plain prose (no multiple-choice picker), what to do with the group:
      - **Keep all** — nothing to change, move to next tag.
      - **Review individually** — walk through each entry and ask per-entry action.
      - **Archive all** — archive every learning in this group (rare; useful for retired tech).

   c. For per-entry review, the actions are:
      - **Keep** — do nothing.
      - **Archive** — retire without replacement. Run:

        ```bash
        python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py archive-learning <ID>
        ```

      - **Supersede** — user provides updated content; you write a replacement that archives the old one and carries `supersedes: <old-ID>` in its frontmatter:

        ```bash
        python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py supersede <old-ID> --content "<new content>"
        ```

        (Add `--infer` if you want `--tags`/`--area` inferred via `claude -p`; without it the replacement is written untagged and `retrotag` can pick it up later.)

      - **Merge** — combine two related learnings into one. Two step flow:
        1. Write a new learning that captures both insights: `notes.py learn --content "<merged content>"`.
        2. Archive the two originals: `notes.py archive-learning <old-ID-1>` and `notes.py archive-learning <old-ID-2>`.

4. Handle the `untagged` group specially — prompt the user whether to auto-tag it in one pass instead of walking one-by-one. `retrotag` calls a model once per untagged learning, so offer `--dry-run` first if the group is large:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py retrotag --dry-run
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py retrotag
   ```

5. When every tag group has been processed, stamp the review timestamp so the startup nudge quiets down:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" scribe/notes.py mark-reviewed
   ```

   Always go through the launcher: it exports `APIARY_TARGET_STATE_DIR`, so the marker lands in the per-target state dir the startup banner actually reads.

6. Report the summary to the user: N kept, M archived, K superseded.

## Notes

- This skill is opt-in — it's never auto-triggered. The startup banner nudges the user after 30 days with `• last review Nd ago — run /review-learnings`.
- If the user wants to bail mid-review, stamping `last_review` anyway would suppress the nudge for another 30 days. Ask whether to stamp on early exit.
- Do not archive or supersede without explicit user approval per entry — changes to the learning corpus are hard to reverse via the CLI (archive is reversible only by manual file moves today).
