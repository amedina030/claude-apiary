# Budgeter Log Toggle

Toggles token usage logging on or off.

When enabled, the budgeter records token consumption for every monitored tool call to `budgeter/data/usage_log.jsonl`.

## Steps

1. Run this command:
   ```bash
   [ -f ~/.claude/budgeter-log-enabled ] && rm ~/.claude/budgeter-log-enabled && echo "OFF" || (echo "enabled" > ~/.claude/budgeter-log-enabled && echo "ON")
   ```
2. If the output is `ON`: report "Budgeter logging is now **ON**. Token usage will be recorded."
3. If the output is `OFF`: report "Budgeter logging is now **OFF**. Token usage will not be recorded."
