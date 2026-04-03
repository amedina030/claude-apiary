# Clarifier Toggle

Toggles the clarifier (assumption detector) on or off.

When enabled, agents can spawn the clarifier to detect assumptions in specs, prompts, or plans before acting on them. The clarifier reports assumptions as JSON — it never resolves them. The caller decides what to do.

## Steps

1. Check whether the file `~/.claude/clarifier-enabled` exists.
2. If it **exists** (clarifier is currently ON):
   - Delete the file `~/.claude/clarifier-enabled`
   - Report: "Clarifier is now **OFF**."
3. If it **does not exist** (clarifier is currently OFF):
   - Create the file `~/.claude/clarifier-enabled` with the content: `enabled`
   - Report: "Clarifier is now **ON**. Agents will run assumption detection before acting."
