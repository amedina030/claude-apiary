# Budgeter Session Warn Toggle

Toggles the session-length nudge on or off.

When enabled, the budgeter injects a one-shot advisory into the conversation when the current prompt size (input + cache_read tokens) crosses configured thresholds — suggesting Claude wrap up at a natural checkpoint and prompt the user to start a fresh session. Gated separately from `/budgeter-warn` so you can opt into context-fill nudges without also re-enabling the magnitude-cost warning.

## Steps

1. Run this command:
   ```bash
   [ -f ~/.claude/budgeter-session-warn-enabled ] && rm ~/.claude/budgeter-session-warn-enabled && echo "OFF" || (echo "enabled" > ~/.claude/budgeter-session-warn-enabled && echo "ON")
   ```
2. If the output is `ON`: report "Budgeter session-length nudge is now **ON**. Claude will suggest wrapping up once the session's prompt size crosses the configured soft/hard thresholds."
3. If the output is `OFF`: report "Budgeter session-length nudge is now **OFF**. Long sessions will continue without a wrap-up suggestion."
