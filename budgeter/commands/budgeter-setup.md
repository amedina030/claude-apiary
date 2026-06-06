# Budgeter Setup

Bootstrap apiary into the current repo so budgeter (and the rest of the
toolkit) works here.

## Steps

1. Resolve the repo the user wants to bootstrap. While still in the
   session's repo, capture its root:
   ```bash
   git rev-parse --show-toplevel
   ```
   This is usually the repo to bootstrap. If the user wants to target a
   different repo, ask which path. Note the resolved absolute path — you
   pass it to `--target` below. (Do **not** use `$CLAUDE_PROJECT_DIR`: it
   is a hooks-only variable and is empty in the tool shell.)

2. Bootstrap from main-apiary. The user must have main-apiary checked
   out and `poetry install`'d. From inside main-apiary, pass the absolute
   target path you captured in step 1:
   ```bash
   poetry run apiary install --target "<path-from-step-1>"
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
