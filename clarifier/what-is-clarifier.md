# What Is Clarifier?

Clarifier is an automatic ambiguity detection and resolution system for Claude Code. When enabled, it intercepts your requests before Claude acts on them, identifies anything that could be interpreted multiple ways, and works interactively with you to resolve the ambiguity — before any work begins.

## The Problem It Solves

Claude is capable but tends to make assumptions when a request is unclear. It picks one interpretation and runs with it. For consequential tasks — editing documents, restructuring code, deleting content — a wrong assumption can mean redoing work or losing something important.

Clarifier inserts a checkpoint between "you ask" and "Claude acts", specifically for requests where the assumptions matter.

## How It Works

When you submit a request, Claude checks whether it is **trivial** or **non-trivial**:

### Trivial requests (clarifier does NOT apply)

A request is trivial only if it meets **all four** of these conditions:

1. **Zero assumptions required** — Claude can begin immediately without guessing anything consequential
2. **Single, clearly identified target** — no judgment call about what to change, where, or how much
3. **Easily undone in one step** — the action is reversible with a single undo or edit
4. **Zero or one explicitly named file affected** — the file is specified by you, not inferred

If a request fails *any one* of these, it is non-trivial.

### Non-trivial requests with ambiguity

If the request is non-trivial *and* contains ambiguity, the clarifier fires. Here is what happens:

**Round-by-round clarification**
1. Claude does not begin the task
2. The clarifier agent takes over, receiving the original prompt, Claude's interpretation, detected ambiguities, and intended plan
3. The clarifier asks you targeted, numbered questions — only what is genuinely needed
4. You answer; the clarifier incorporates your answers into a revised prompt
5. The clarifier re-checks the updated prompt for remaining ambiguity and repeats if needed

**Iteration limit**
After 5 rounds of back-and-forth, if ambiguity remains, the clarifier pauses and shows you the current state of the prompt along with what's still unresolved. You can choose to keep going or proceed as-is.

**Non-answer handling**
If you can't or don't want to answer a question, the clarifier asks whether to proceed with that ambiguity unresolved, or keep working on it. It will not proceed without your explicit say-so.

**Final approval**
Once the prompt is clean (or you've granted permission to proceed), the clarifier shows you the final version of the prompt it will hand to Claude and asks for your explicit approval. Claude does not act until you confirm.

**Session logging**
After every approval, the clarifier saves a log of the session — all rounds of questions and answers, the final approved prompt, and any unresolved ambiguities — to `.claude/clarifier-logs/` in your project or `~/.claude/clarifier-logs/` as a fallback.

Log filenames use the format `clarifier-YYYY-MM-DD-HHMMSS-XXXX.md` (timestamp + 4 random hex chars) to stay human-readable and avoid collisions when multiple sessions run concurrently. Each log also contains a UUIDv7 that serves as its canonical unique identifier.

After the clarifier returns, the executing agent appends a cost entry to `~/.claude/clarifier-logs/cost.log` containing the token usage, duration, and a reference to the session log by both filename and UUIDv7 — so you can cross-reference cost data with full session details at any time.

### Non-trivial requests without ambiguity

If the request is non-trivial but unambiguous, Claude proceeds normally. The clarifier only fires when there is genuine ambiguity.

## What Counts as Ambiguity

- The request has two or more meaningfully different valid interpretations
- The scope is unclear (how much, how far, which parts)
- Claude would need to make a consequential assumption to proceed
- The intended outcome is not specific enough to verify completion
- Claude's intended plan might not match what you actually want

## The Toggle

Clarifier is opt-in. Use `/clarifier` at any time to turn it on or off. When off, Claude will still flag potential ambiguity to you in its response, but will not automatically intercept and block requests.

## Example

**Request:** `Clean up the open questions document.`

With clarifier OFF, Claude might pick an interpretation and start editing. With clarifier ON:

1. Clarifier asks: what does "clean up" mean? which questions? remove resolved ones?
2. You answer
3. Clarifier shows you the refined prompt: *"Reformat OPEN_QUESTIONS.md for consistent heading style and remove questions marked as resolved. Do not change content otherwise."*
4. You approve
5. Claude acts on that — not your original vague request
