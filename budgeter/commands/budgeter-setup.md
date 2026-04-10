# Budgeter Setup

Sets up claude-apiary budgeter hooks for the current project.

## Steps

1. Use the current working directory as the project path to configure.

2. Run setup via the launcher:
   ```bash
   python ~/.claude/apiary_launch.py setup.py --project-path "<current-working-directory>"
   ```

   If the launcher fails (e.g. `~/.claude/apiary_launch.py` does not exist), the global install has not been run yet. Ask the user for the apiary repo path and run `python <path>/setup.py --global` first.

3. Report what was configured: the settings.json path, the Python executable used, and the claude-apiary location.

4. Remind the user to start a new Claude Code session for the hooks to activate.
