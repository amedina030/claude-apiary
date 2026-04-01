## Clarifier

At the start of every session, check whether `~/.claude/clarifier-enabled` exists.
- If it exists: the clarifier is **ON**. Note this silently — do not announce it every session.
- If it does not exist: the clarifier is **OFF**.

### When the clarifier is ON
Before acting on any non-trivial user request, assess whether the request contains ambiguity — multiple valid interpretations, unclear scope, implicit assumptions, or missing context that would meaningfully change your approach.

If ambiguity is detected:
- Do not begin the task.
- Surface to the clarifier sub-agent: (1) the original prompt, (2) your interpretation of the task, (3) a list of detected ambiguities and why they matter, (4) your intended plan.
- Spawn the clarifier sub-agent. It will ask the user questions, write a partial log, and return with `agentId: [uuid] | log: [filename]`. Note the uuid and log filename. Then call:
  `python ~/.claude/clarifier/log_cost.py tally --id [uuid] --tokens [total_tokens] --tools [tool_uses] --duration [duration_ms]`
- The clarifier is multi-turn. After it returns with questions, wait for the user's response, then **resume** the clarifier (using its agentId) passing the user's answers. After each resume, call `log_cost.py tally` again with that call's metadata.
- Continue resuming until the clarifier returns `CLARIFIER_DONE`. At that point extract the final approved prompt from the clarifier's return message.
- Call: `python ~/.claude/clarifier/log_cost.py finalize --id [uuid] --log [log filename] --prompt "[original prompt]" --session-id [session_id from budgeter context]`
- Execute the task using the final approved prompt, not the original.

If no ambiguity is detected: proceed normally.

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
