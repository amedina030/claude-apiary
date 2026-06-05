# Budgeter Setup

Bootstrap apiary into the current repo so budgeter (and the rest of the
toolkit) works here.

## Steps

1. Confirm `$CLAUDE_PROJECT_DIR` resolves to the repo the user wants to
   bootstrap (usually it does — it's the session's repo). If the user
   wants to target a different repo, ask which path.

2. Bootstrap from main-apiary. The user must have main-apiary checked
   out and `poetry install`'d. From inside main-apiary:
   ```bash
   poetry run apiary install --target "$CLAUDE_PROJECT_DIR"
   ```

   If the user doesn't know main-apiary's location, ask them. Apiary's
   per-repo install needs main-apiary's source tree to copy slash
   commands and the launcher template from.

3. Report what was configured:
   - The bootstrapped repo's slug and uid
   (printed by `apiary install`).
   - The state directory at `<main-apiary>/.repos/<slug>/`.

4. Remind the user to start a new Claude Code session in the
   bootstrapped repo for the per-repo hooks to activate.
