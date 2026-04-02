## Clarifier

At the start of every session, check whether `~/.claude/clarifier-enabled` exists.
- If it exists: the clarifier is **ON**. Note this silently — do not announce it every session.
- If it does not exist: the clarifier is **OFF**.

### When the clarifier is ON
For every non-trivial user request, spawn the clarifier sub-agent before beginning work. Do not assess ambiguity yourself — let the clarifier make that determination.

- Do not begin the task.
- Surface to the clarifier sub-agent: (1) the original prompt, (2) your interpretation of the task, (3) any potential ambiguities you notice (this is context for the clarifier, not a gate), (4) your intended plan.
- Spawn the clarifier sub-agent. It will either:
  - **Find no ambiguity** → return silently with `CLARIFIER_DONE` and the original prompt unchanged. No user interaction needed.
  - **Find ambiguity** → ask the user questions, write a partial log, and return with `agentId: [uuid] | log: [filename]`. Note the uuid and log filename. Then call:
    `python ~/.claude/clarifier/log_cost.py tally --id [uuid] --tokens [total_tokens] --tools [tool_uses] --duration [duration_ms]`
- If the clarifier found ambiguity (multi-turn): after it returns with questions, wait for the user's response, then **resume** the clarifier (using its agentId) passing the user's answers. After each resume, call `log_cost.py tally` again with that call's metadata.
- Continue resuming until the clarifier returns `CLARIFIER_DONE`. At that point extract the final approved prompt from the clarifier's return message.
- Call: `python ~/.claude/clarifier/log_cost.py finalize --id [uuid] --log [log filename] --prompt "[original prompt]" --session-id [session_id from budgeter context]`
- Execute the task using the final approved prompt, not the original.

For trivial tasks (see below), use your judgment: if you notice potential ambiguity, you may spawn the clarifier. If not, proceed normally.

### When the clarifier is OFF
If you detect potential ambiguity in a user request, do not run the clarifier automatically. Instead, flag it to the user:

"I noticed some ambiguity in this request. You can enable the clarifier with `/clarifier` to have it resolved interactively before I proceed, or clarify directly and I'll continue."

Then wait for the user's response before proceeding.

### What counts as trivial (clarifier does NOT apply)
A request is trivial only if it meets **all four** of the following conditions:

1. **Zero assumptions required** — you can begin immediately without assuming anything consequential about intent, scope, or approach
2. **Single, clearly identified target** — there is no judgment call about what to change, where, or how much
3. **Easily undone in one step** — the action is reversible with a single undo or edit
4. **Zero or one explicitly named file affected** — the file is specified by the user, not inferred

If the request fails **any one** of these four conditions, it is non-trivial and the clarifier applies.

### What counts as ambiguity
- The request has two or more meaningfully different valid interpretations
- The scope is unclear (how much, how far, which parts)
- You would need to make a consequential assumption to proceed
- The intended outcome is not specific enough to verify completion
- Your intended plan might not match what the user actually wants

## Toggle Command
Use `/clarifier` to toggle the clarifier on or off at any time.

## Scribe — Structured Notes

The scribe tool (`scribe/notes.py`) manages operational notes for cross-session continuity.

### When to write notes

Notes are primarily for Claude's own use — to maintain continuity across sessions.

**User-triggered:**

| Signal | Type | Action |
|--------|------|--------|
| User defers work ("later", "hold", "next time") | todo | Write a TODO with enough context to resume |
| Design choice resolved, alternatives rejected | decision | Record what was decided and what was rejected |
| Something blocks progress | blocker | Record what's blocked and why |
| User says "note this", "write that down" | as specified | Write the note with the type the user indicates, or `context` |
| Wishlist idea ("would be nice", "eventually") | wishlist | Record the idea |
| Work that matches an active TODO is completed | — | Run `notes.py done <id>` |

**Self-triggered (Claude writes without user prompting):**

| Signal | Type | Action |
|--------|------|--------|
| You defer a side-fix to stay on task | todo | Record what you noticed and why you deferred it |
| You observe a bug unrelated to current work | todo or blocker | Record the bug, severity, and where you saw it |
| You left something incomplete (edge cases, error handling) | todo | Record what's missing and where |
| Context is getting large, key state at risk of compaction | context | Save critical decisions, open questions, and current approach |
| Something needs verification in a future session | todo | Record what changed and what should be checked |

**Do not** write notes for:
- Routine tool calls or file reads
- Information that belongs in memory (permanent facts about user/project)
- Things already captured by git (file changes, commit history)
- Ephemeral conversation details that won't matter next session

### Memory vs Notes

- **Memory** (`~/.claude/projects/.../memory/`): permanent facts — user preferences, project structure, team info. Still true in 3 months.
- **Notes** (`~/.claude/notes.jsonl` via `scribe/notes.py`): operational state — deferred work, session context, decisions in flux. Relevant to current work, decays over time.

If it's still true in 3 months → memory. If it's about current work → note.

### Archive fallback

If a note the user references isn't found in active notes, search the archive:
```
python scribe/notes.py list --archive --search "<keyword>"
```
