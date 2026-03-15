# Budgeter Warn Toggle

Toggles the cost estimation warning on or off.

When enabled, the budgeter intercepts monitored tool calls, estimates token cost based on similar past tasks, and warns Claude to ask the user before proceeding if the call looks expensive.

## Steps

1. Run this command:
   ```bash
   [ -f ~/.claude/budgeter-warn-enabled ] && rm ~/.claude/budgeter-warn-enabled && echo "OFF" || (echo "enabled" > ~/.claude/budgeter-warn-enabled && echo "ON")
   ```
2. If the output is `ON`: report "Budgeter warnings are now **ON**. Claude will ask before running calls expected to be expensive."
3. If the output is `OFF`: report "Budgeter warnings are now **OFF**. Expensive calls will proceed without prompting."
