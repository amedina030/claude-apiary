# Clarifier Toggle

Toggles the clarifier agent on or off.

When enabled, the clarifier automatically intercepts requests where ambiguity is detected and works interactively with you to resolve it before the executing agent proceeds.

## Steps

1. Check whether the file `~/.claude/clarifier-enabled` exists.
2. If it **exists** (clarifier is currently ON):
   - Delete the file `~/.claude/clarifier-enabled`
   - Report: "Clarifier is now **OFF**. Claude will suggest enabling it if ambiguity is detected."
3. If it **does not exist** (clarifier is currently OFF):
   - Create the file `~/.claude/clarifier-enabled` with the content: `enabled`
   - Report: "Clarifier is now **ON**. It will automatically run when ambiguity is detected in your requests."
