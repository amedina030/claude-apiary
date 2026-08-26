---
name: budgeter
description: Toggle a budgeter feature for this repo — log, warn, or session-warn
user-invocable: true
---

# /budgeter — feature toggles

Turn one budgeter feature on or off for the current repo. Takes a single
argument: `log`, `warn`, or `session-warn`.

| Argument | Flag name | What it gates |
|----------|-----------|---------------|
| `log` | `budgeter-log` | Token-usage logging — records what every monitored tool call cost |
| `warn` | `budgeter-warn` | Cost-estimation warning — Claude asks before a call that looks expensive |
| `session-warn` | `budgeter-session-warn` | Session-length nudge — suggests wrapping up once the prompt size crosses the configured thresholds |

Each flag is a sentinel file at `<repo>/.claude/apiary/flags/<flag-name>-enabled`.
Toggles are per-repo and persist across sessions.

## Steps

1. Read the argument the user typed after `/budgeter`. If they gave none, or
   one that isn't in the table above, show them the table and ask which they
   meant — do not guess, and do not toggle anything.

2. Toggle it, substituting the **Flag name** column for `<flag-name>`:

   ```bash
   python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" core/flags.py toggle <flag-name>
   ```

   The command prints the flag's **new** state — `ON` or `OFF` — and exits 0.
   Exit 1 with an `error:` line on stderr means no bootstrapped repo is in
   scope: tell the user to run `/budgeter-setup` for this repo instead of
   retrying the toggle.

3. Report the new state, using the line that matches what was toggled:

   - `log` **ON** — "Budgeter logging is now **ON**. Token usage will be recorded."
   - `log` **OFF** — "Budgeter logging is now **OFF**. Token usage will not be recorded."
   - `warn` **ON** — "Budgeter warnings are now **ON**. Claude will ask before running calls expected to be expensive."
   - `warn` **OFF** — "Budgeter warnings are now **OFF**. Expensive calls will proceed without prompting."
   - `session-warn` **ON** — "Budgeter session-length nudge is now **ON**. Claude will suggest wrapping up once the session's prompt size crosses the configured soft/hard thresholds."
   - `session-warn` **OFF** — "Budgeter session-length nudge is now **OFF**. Long sessions will continue without a wrap-up suggestion."

## Reading or setting a state explicitly

Same CLI, different verb — use these when the user asks "is X on?" or asks for
a specific state rather than a flip:

```bash
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" core/flags.py status  budgeter-log
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" core/flags.py enable  budgeter-log
python "$(git rev-parse --show-toplevel)/.claude/apiary/launch.py" core/flags.py disable budgeter-log
```

`status` never changes anything; `enable`/`disable` are idempotent.
